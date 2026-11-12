"""D1 Ryu controller: learning switch + per-UDP-flow rules + flow stats
logging + REST endpoint (POST /test) for the socket application.

Run:  ryu-manager sdn/controller.py
Env:  RESULTS_DIR (default results/live), CONTROLLER_MODE (static|dynamic)
"""
import csv
import json
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER,
                                    DEAD_DISPATCHER, set_ev_cls)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import ether_types
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, udp, ether_types
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response

UDP_PRIO = 20     # per-flow measurement rules (counted and logged)
L2_PRIO = 10      # generic L2 rules
POLL_S = 1        # flow stats poll interval
UDP_IDLE_S = 10   # UDP flow expires 10 s after traffic stops
FRAME_OVERHEAD = 42  # UDP 8 + IPv4 20 + Ethernet 14 bytes per packet
OVER_LIMIT_POLLS = 3  # polls in a row above target before limiting (ignores one-off spikes)

FLOW_HDR = ['ts', 'dpid', 'flow_id', 'pkts', 'bytes', 'duration_s',
            'delta_pkts', 'delta_bytes', 'rate_mbps']
EVENT_HDR = ['ts', 'event', 'flow_id', 'detail']


class TestApi(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data['app']

    @route('test', '/test', methods=['POST'])
    def post_test(self, req, **kwargs):
        try:
            body = json.loads(req.body)
        except ValueError:
            return Response(status=400, body=b'bad json')
        self.app.on_test_event(body)
        return Response(status=200, content_type='application/json',
                        body=b'{"ok": true}')


class BwMeter(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = os.environ.get('CONTROLLER_MODE', 'static')
        self.out_dir = os.environ.get('RESULTS_DIR', 'results/live')
        os.makedirs(self.out_dir, exist_ok=True)
        self.mac_to_port = {}
        self.datapaths = {}
        self.prev = {}      # (dpid, flow_id) -> (pkts, bytes, ts)
        self.tests = {}
        self.flow_meters = {}
        self.next_meter_ids = {}
        self.rate_limited = {}
        self.low_rate_polls = {}
        self.recent_installs = {}   # (dpid, flow_id, in_port) -> time a UDP rule was sent
        self.high_rate_polls = {}
        kwargs['wsgi'].register(TestApi, {'app': self})
        self.monitor_thread = hub.spawn(self._monitor)
        self.logger.info('Controller started, mode=%s, out=%s',
                         self.mode, self.out_dir)

    # ---------- helpers ----------
    def _append(self, name, header, row):
        path = os.path.join(self.out_dir, name)
        new = not os.path.exists(path)
        with open(path, 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)

    def _event(self, event, flow_id, detail):
        self._append('controller_events.csv', EVENT_HDR,
                     [time.time(), event, flow_id, detail])

    @staticmethod
    def _fid(src, dst, port):
        return '%s>%s:%s' % (src, dst, port)
    def _is_registered_test_flow(self, src, dst, dst_port):
        for body in reversed(list(self.tests.values())):
            if (body.get('src') == src and
                body.get('dst') == dst and
                int(body.get('dst_port', -1)) == int(dst_port)):
                return True
        return False
    def _target_rate_kbps(self, src, dst, dst_port):
        """Declared target as an on-the-wire rate in kbit/s.

        The application declares its rate in UDP payload bytes, but switch
        counters and OpenFlow meters count whole Ethernet frames (payload + 42
        bytes of UDP/IP/Ethernet headers). Convert, so a flow that sends exactly
        what it declared is never limited (at 200 B payload the headers alone
        add 21 %).
        """
        for body in reversed(list(self.tests.values())):
            if (body.get('src') == src and
                body.get('dst') == dst and
                int(body.get('dst_port', -1)) == int(dst_port)):
                rate = float(body.get('target_rate_mbps', 0))
                size = float(body.get('payload_size', 0) or 0)
                if rate > 0:
                    if size > 0:
                        rate *= (size + FRAME_OVERHEAD) / size
                    return max(1, int(rate * 1000))

        return int(os.environ.get('METER_RATE_KBPS', '5000'))

    def _recently_installed(self, dpid, fid, in_port):
        """True if we already sent this UDP rule less than a second ago.

        Packets that reached the controller before the rule existed are still
        queued as packet-ins. Re-sending the same FlowMod for each of them keeps
        the controller busy (delaying flow-stats replies) and resets the rule's
        duration, so forward those packets without re-installing.
        """
        key = (dpid, fid, in_port)
        now = time.time()
        if now - self.recent_installs.get(key, 0) < 1.0:
            return True
        self.recent_installs[key] = now
        return False

    def add_flow(self, dp, priority, match, actions, idle=0, flags=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst,
                                      idle_timeout=idle, flags=flags))

    def add_meter(self, dp, meter_id, rate_kbps):
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        bands = [
            parser.OFPMeterBandDrop(
                rate=rate_kbps,
                burst_size=0
            )
        ]

        request = parser.OFPMeterMod(
            datapath=dp,
            command=ofp.OFPMC_ADD,
            flags=ofp.OFPMF_KBPS,
            meter_id=meter_id,
            bands=bands
        )

        dp.send_msg(request)
    def modify_meter(self, dp, meter_id, rate_kbps):
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        bands = [
            parser.OFPMeterBandDrop(
                rate=rate_kbps,
                burst_size=0
            )
        ]

        request = parser.OFPMeterMod(
            datapath=dp,
            command=ofp.OFPMC_MODIFY,
            flags=ofp.OFPMF_KBPS,
            meter_id=meter_id,
            bands=bands
        )

        dp.send_msg(request)

    # ---------- REST callback ----------
    def on_test_event(self, body):
        ev = body.get('event', 'unknown')
        tid = body.get('test_id')
        if ev == 'test_start':
            self.tests[tid] = body
        fid = self._fid(body.get('src', '?'), body.get('dst', '?'),
                        body.get('dst_port', '?'))
        self._event(ev, fid, json.dumps(body))
        self.logger.info('REST %s: %s', ev, body)

    # ---------- switch setup ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, parser.OFPMatch(), actions)   # table-miss

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        else:
            self.datapaths.pop(dp.id, None)

    # ---------- learning switch ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dpid = dp.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        out_port = self.mac_to_port[dpid].get(
            eth.dst, ofp.OFPP_FLOOD
        )
        actions = [parser.OFPActionOutput(out_port)]

        # Unknown destination: flood the packet so MAC learning can continue.
        if out_port == ofp.OFPP_FLOOD:
            dp.send_msg(parser.OFPPacketOut(
                datapath=dp,
                buffer_id=ofp.OFP_NO_BUFFER,
                in_port=in_port,
                actions=actions,
                data=msg.data
            ))
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        u = pkt.get_protocol(udp.udp)

        # Dynamic mode: install a metered rule for UDP traffic.
        if ip and u and self.mode == 'dynamic':
            match = parser.OFPMatch(
                in_port=in_port,
                eth_type=0x0800,
                ip_proto=17,
                ipv4_src=ip.src,
                ipv4_dst=ip.dst,
                udp_dst=u.dst_port
            )

            if not hasattr(self, '_metered_dpids'):
                self._metered_dpids = set()

            try:
                fid = self._fid(ip.src, ip.dst, u.dst_port)
                meter_key = (dp.id, fid)

        # Start with a permissive meter.
                # The stats handler will activate the target limit when needed.
                rate_kbps = 1000000

                meter_id = self.flow_meters.get(meter_key)

                if meter_id is None:
                    meter_id = self.next_meter_ids.get(dp.id, 1)

                    if meter_id > 65535:
                        raise RuntimeError("No available meter IDs")

                    self.next_meter_ids[dp.id] = meter_id + 1

                    self.add_meter(dp, meter_id, rate_kbps)
                    self.flow_meters[meter_key] = meter_id

                    self.logger.info(
                        "METER INSTALL REQUEST dpid=%s meter=%s rate=%s Kbps",
                        dp.id, meter_id, rate_kbps
                    )

                instructions = [
                    parser.OFPInstructionMeter(meter_id),
                    parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, actions
                    )
                ]

                if not self._recently_installed(dp.id, fid, in_port):
                    dp.send_msg(parser.OFPFlowMod(
                        datapath=dp,
                        priority=UDP_PRIO,
                        match=match,
                        instructions=instructions,
                        idle_timeout=UDP_IDLE_S,
                        flags=ofp.OFPFF_SEND_FLOW_REM
                    ))

                self.logger.info(
                    "METERED UDP FLOW dpid=%s %s:%s -> %s:%s",
                    dp.id, ip.src, u.src_port, ip.dst, u.dst_port
                )

            except Exception:
                self.logger.exception(
                    "Failed to install dynamic meter or UDP flow"
                )

                # Forward this packet, but do not install an unmetered
                # UDP flow that would bypass the bandwidth limit.
                dp.send_msg(parser.OFPPacketOut(
                    datapath=dp,
                    buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=in_port,
                    actions=actions,
                    data=msg.data
                ))
                return

        elif ip and u:
            # Static mode: one rule per UDP flow so its packet/byte counters
            # are polled, logged and reported when the flow is removed.
            udp_match = parser.OFPMatch(
                eth_type=0x0800,
                ip_proto=17,
                ipv4_src=ip.src,
                ipv4_dst=ip.dst,
                udp_dst=u.dst_port
            )
            if not self._recently_installed(
                    dp.id, self._fid(ip.src, ip.dst, u.dst_port), 0):
                self.add_flow(
                    dp, UDP_PRIO, udp_match, actions,
                    idle=UDP_IDLE_S, flags=ofp.OFPFF_SEND_FLOW_REM
                )

        elif self.mode != 'dynamic':
            # Non-UDP traffic in static mode: ordinary learning-switch flows.
            # Match the packet type too (ARP, or the IP protocol such as ICMP).
            # A plain MAC-pair rule would also catch later UDP packets between
            # the same hosts, so they would never reach the controller, no
            # per-UDP-flow rule would be installed and flowstats.csv stays empty.
            fields = dict(in_port=in_port, eth_src=eth.src, eth_dst=eth.dst,
                          eth_type=eth.ethertype)
            if ip:
                fields['ip_proto'] = ip.proto
            general_match = parser.OFPMatch(**fields)
            self.add_flow(
                dp, 10, general_match, actions, idle=30
            )

        # In dynamic mode, do not install general Ethernet flows:
        # they could bypass the UDP meter. Forward this packet now.
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=msg.data
        ))

    # ---------- flow stats polling ----------
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))
            hub.sleep(POLL_S)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _stats_reply(self, ev):
        now = time.time()
        dp = ev.msg.datapath
        dpid = dp.id

        for st in ev.msg.body:
            if st.priority != UDP_PRIO:
                continue

            m = st.match
            src = m['ipv4_src']
            dst = m['ipv4_dst']
            port = m['udp_dst']

            fid = self._fid(src, dst, port)
            key = (dpid, fid)

            p0, b0, t0 = self.prev.get(key, (0, 0, None))

            if st.byte_count < b0:
                p0, b0 = 0, 0

            dpk = st.packet_count - p0
            dby = st.byte_count - b0

            if t0 is None:
                # First sample of this rule: no interval to measure over yet.
                rate = 0.0
            else:
                dt = now - t0
                if dt < 0.5 * POLL_S:
                    # Several replies arrived together (the controller was busy,
                    # e.g. with a burst of packet-ins). Dividing by this tiny gap
                    # gives absurd rates (thousands of Mbit/s), so skip this one;
                    # the next normal reply covers the whole interval.
                    continue
                rate = dby * 8 / dt / 1e6

            self.prev[key] = (st.packet_count, st.byte_count, now)

            self._append(
                'flowstats.csv', FLOW_HDR,
                [now, dpid, fid, st.packet_count, st.byte_count,
                 st.duration_sec, dpk, dby, round(rate, 3)]
            )

            if t0 is None or not self._is_registered_test_flow(src, dst, port):
                continue

            target = self._target_rate_kbps(src, dst, port) / 1000.0
            meter_id = self.flow_meters.get(key)

            if meter_id is None:
                continue

            is_limited = self.rate_limited.get(key, False)

            # Count consecutive polls over target. One poll can read high just
            # because two stats replies arrived close together (short dt).
            if rate > target * 1.20 and not is_limited:
                self.high_rate_polls[key] = self.high_rate_polls.get(key, 0) + 1
            else:
                self.high_rate_polls[key] = 0

            if self.high_rate_polls[key] >= OVER_LIMIT_POLLS and not is_limited:
                self.high_rate_polls[key] = 0
                self.modify_meter(dp, meter_id, int(target * 1000))
                self.rate_limited[key] = True
                self.low_rate_polls[key] = 0

                self._event(
                    'rate_limit_on', fid,
                    'measured=%.3f Mbps target=%.3f Mbps meter=%s' %
                    (rate, target, meter_id)
                )

                self.logger.info(
                    'RATE LIMIT ON dpid=%s flow=%s measured=%.3f target=%.3f',
                    dpid, fid, rate, target
                )

            elif is_limited:
                if 0 < rate < target * 0.50:
                    self.low_rate_polls[key] = (
                        self.low_rate_polls.get(key, 0) + 1
                    )
                else:
                    self.low_rate_polls[key] = 0

                if self.low_rate_polls[key] >= 5:
                    self.modify_meter(dp, meter_id, 1000000)
                    self.rate_limited[key] = False
                    self.low_rate_polls[key] = 0

                    self._event(
                        'rate_limit_off', fid,
                        'measured=%.3f Mbps target=%.3f Mbps meter=%s' %
                        (rate, target, meter_id)
                    )

                    self.logger.info(
                        'RATE LIMIT OFF dpid=%s flow=%s',
                        dpid, fid
                    )

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed(self, ev):
        msg = ev.msg
        if msg.priority != UDP_PRIO:
            return
        m = msg.match
        fid = self._fid(m['ipv4_src'], m['ipv4_dst'], m['udp_dst'])
        self._event('flow_removed', fid,
                    'dpid=%s pkts=%s bytes=%s duration=%s' % (
                        msg.datapath.id, msg.packet_count,
                        msg.byte_count, msg.duration_sec))
        self.prev.pop((msg.datapath.id, fid), None)
    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def _openflow_error(self, ev):
        msg = ev.msg
        self.logger.error(
            'OPENFLOW ERROR dpid=%s type=%s code=%s data=%s',
            msg.datapath.id,
            msg.type,
            msg.code,
            msg.data.hex()
        )

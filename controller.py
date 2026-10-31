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
                meter_id = 1
                rate_kbps = int(
                    os.environ.get('METER_RATE_KBPS', '5000')
                )

                # Install the meter only once per switch.
                if dp.id not in self._metered_dpids:
                    self.add_meter(dp, meter_id, rate_kbps)
                    self._metered_dpids.add(dp.id)
                    self.logger.info(
                        "METER INSTALL REQUEST dpid=%s rate=%s Kbps",
                        dp.id, rate_kbps
                    )

                instructions = [
                    parser.OFPInstructionMeter(meter_id),
                    parser.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, actions
                    )
                ]

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
            self.add_flow(
                dp, UDP_PRIO, udp_match, actions,
                idle=UDP_IDLE_S, flags=ofp.OFPFF_SEND_FLOW_REM
            )

        elif self.mode != 'dynamic':
            # Non-UDP traffic in static mode: ordinary learning-switch flows.
            general_match = parser.OFPMatch(
                in_port=in_port,
                eth_src=eth.src,
                eth_dst=eth.dst
            )
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
        dpid = ev.msg.datapath.id
        for st in ev.msg.body:
            if st.priority != UDP_PRIO:
                continue
            m = st.match
            fid = self._fid(m['ipv4_src'], m['ipv4_dst'], m['udp_dst'])
            key = (dpid, fid)
            p0, b0, t0 = self.prev.get(key, (0, 0, None))
            if st.byte_count < b0:          # flow was re-installed
                p0, b0 = 0, 0
            dpk, dby = st.packet_count - p0, st.byte_count - b0
            dt = (now - t0) if t0 else POLL_S
            rate = dby * 8 / dt / 1e6 if dt > 0 else 0.0
            self.prev[key] = (st.packet_count, st.byte_count, now)
            self._append('flowstats.csv', FLOW_HDR,
                         [now, dpid, fid, st.packet_count, st.byte_count,
                          st.duration_sec, dpk, dby, round(rate, 3)])

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


import csv
import json
import os
import time
import threading

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib import hub
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from webob import Response


class TestAPI(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.controller = data['controller']

    @route('test_api', '/test', methods=['POST'])
    def test(self, req, **kwargs):
        try:
            body = req.json if req.body else {}
            event = body.get('event', body.get('type', 'unknown'))
            test_id = body.get('test_id', 'unknown')
            target = body.get('target_rate_mbps', '')

            self.controller.log_event(event, test_id, target, body)

            return Response(
                content_type='application/json',
                body=json.dumps({'status': 'ok'}).encode()
            )
        except Exception as exc:
            return Response(
                status=400,
                content_type='application/json',
                body=json.dumps({'error': str(exc)}).encode()
            )


class BandwidthController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        wsgi = kwargs['wsgi']
        wsgi.register(TestAPI, {'controller': self})

        self.mac_to_port = {}
        self.datapaths = {}
        self.previous_stats = {}
        self.mode = os.getenv('CONTROLLER_MODE', 'static')

        os.makedirs('results', exist_ok=True)

        self.stats_file = open(
            'results/flowstats.csv', 'a', newline=''
        )
        self.stats_writer = csv.writer(self.stats_file)

        if self.stats_file.tell() == 0:
            self.stats_writer.writerow([
                'timestamp', 'switch', 'in_port', 'out_port',
                'packets', 'bytes', 'delta_pkts', 'delta_bytes',
                'rate_mbps'
            ])

        self.events_file = open(
            'results/controller_events.csv', 'a', newline=''
        )
        self.events_writer = csv.writer(self.events_file)

        if self.events_file.tell() == 0:
            self.events_writer.writerow([
                'timestamp', 'event', 'test_id', 'target_rate_mbps', 'data'
            ])

        self.monitor_thread = hub.spawn(self._monitor)

    def log_event(self, event, test_id, target, data):
        with threading.Lock():
            self.events_writer.writerow([
                time.time(), event, test_id, target,
                json.dumps(data)
            ])
            self.events_file.flush()

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                   ofproto.OFPCML_NO_BUFFER)
        ]

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions
            )
        ]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            match=match,
            instructions=instructions
        )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change(self, ev):
        dp = ev.datapath

        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        parser = dp.ofproto_parser
        ofproto = dp.ofproto

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dpid = dp.id
        in_port = msg.match['in_port']

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        out_port = self.mac_to_port[dpid].get(
            eth.dst, ofproto.OFPP_FLOOD
        )

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                in_port=in_port,
                eth_src=eth.src,
                eth_dst=eth.dst
            )
            instructions = [
                parser.OFPInstructionActions(
                    ofproto.OFPIT_APPLY_ACTIONS, actions
                )
            ]
            mod = parser.OFPFlowMod(
                datapath=dp,
                priority=10,
                match=match,
                instructions=instructions
            )
            dp.send_msg(mod)

        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data

        out = parser.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )
        dp.send_msg(out)

    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                request = parser.OFPFlowStatsRequest(dp)
                dp.send_msg(request)
            hub.sleep(1)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply(self, ev):
        dp = ev.msg.datapath
        now = time.time()

        for stat in ev.msg.body:
            if stat.priority == 0:
                continue

            key = (dp.id, stat.match.get('in_port'),
                   stat.instructions[0].actions[0].port
                   if stat.instructions and
                   getattr(stat.instructions[0], 'actions', None)
                   else -1)

            packets = stat.packet_count
            byte_count = stat.byte_count
            old = self.previous_stats.get(key, (packets, byte_count, now))

            delta_pkts = max(0, packets - old[0])
            delta_bytes = max(0, byte_count - old[1])
            elapsed = max(now - old[2], 0.001)
            rate_mbps = delta_bytes * 8 / elapsed / 1_000_000

            self.previous_stats[key] = (packets, byte_count, now)

            self.stats_writer.writerow([
                now, dp.id, key[1], key[2], packets, byte_count,
                delta_pkts, delta_bytes, rate_mbps
            ])

        self.stats_file.flush()

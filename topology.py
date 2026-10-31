#!/usr/bin/env python3
"""D1 topology: h1,h3 -- s1 -- s2 -- s3 -- h2, plus a NAT so hosts can
reach the Ryu REST API (running in the root namespace) at 10.0.0.254:8080.

Run:  sudo python3 sdn/topology.py
"""
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def build():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink,
                  autoSetMacs=True, build=False)

    net.addController('c0', controller=RemoteController,
                      ip='127.0.0.1', port=6653)

    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')

    h1 = net.addHost('h1', ip='10.0.0.1/24')  # UDP sender
    h2 = net.addHost('h2', ip='10.0.0.2/24')  # UDP receiver
    h3 = net.addHost('h3', ip='10.0.0.3/24')  # background traffic

    # Access links (fast) and core links (the 20 Mbps bottleneck)
    net.addLink(h1, s1, bw=100)
    net.addLink(h3, s1, bw=100)
    net.addLink(s1, s2, bw=20)
    net.addLink(s2, s3, bw=20)
    net.addLink(h2, s3, bw=100)

    # Gives hosts a route to the root namespace (controller REST API)
    net.addNAT(connect=s1, ip='10.0.0.254/24')

    return net


if __name__ == '__main__':
    setLogLevel('info')
    net = build()
    net.build()
    net.start()
    info('*** Controller REST API reachable from hosts at http://10.0.0.254:8080\n')
    CLI(net)
    net.stop()

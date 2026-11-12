#!/usr/bin/env python3

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def build():
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        build=False
    )

    net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6653
    )

    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')

    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')
    h5 = net.addHost('h5', ip='10.0.0.5/24')

    # Access links
    net.addLink(h1, s1, bw=100)
    net.addLink(h3, s1, bw=100)
    net.addLink(h4, s1, bw=100)
    net.addLink(h2, s3, bw=100)
    net.addLink(h5, s3, bw=100)

    # Core bottleneck links
    net.addLink(s1, s2, bw=20)
    net.addLink(s2, s3, bw=20)

    # REST API connectivity
    net.addNAT(connect=s1, ip='10.0.0.254/24')

    return net


if __name__ == '__main__':
    setLogLevel('info')

    net = build()
    net.build()
    net.start()

    info('*** Five-host topology started\n')
    CLI(net)
    net.stop()

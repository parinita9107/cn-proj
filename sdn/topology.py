
#!/usr/bin/env python3

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel


def main():
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True
    )

    c0 = net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6653
    )

    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')

    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')

    net.addLink(h1, s1, bw=20)
    net.addLink(h3, s1, bw=20)
    net.addLink(s1, s2, bw=20, delay='2ms')
    net.addLink(s2, s3, bw=20, delay='2ms')
    net.addLink(s3, h2, bw=20)

    try:
        net.start()
        print("Network started. Type pingall to test connectivity.")
        CLI(net)
    finally:
        net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    main()

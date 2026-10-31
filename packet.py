"""Packet format shared by sender.py and receiver.py.

20-byte header, network byte order (struct format "!BBHIIQ"):

    magic        1 byte   0xB7, packets with any other value are rejected
    type         1 byte   1 = DATA, 2 = END
    reserved     2 bytes  always 0
    test_id      4 bytes  identifies the test run
    seq          4 bytes  DATA: sequence number starting at 0
                          END : total number of DATA packets the sender produced
    send_ts_ns   8 bytes  sender clock (time.time_ns) when the packet was built

The header is followed by zero padding so that the UDP payload has exactly
the requested size.
"""
import struct
import time

MAGIC = 0xB7
TYPE_DATA = 1
TYPE_END = 2

HEADER = struct.Struct("!BBHIIQ")
HEADER_SIZE = HEADER.size  # 20

# Largest UDP payload that fits in a 1500 byte Ethernet MTU without fragmentation
MAX_PAYLOAD = 1472

# Bytes added by the network around each UDP payload: UDP 8 + IP 20 + Ethernet 14
FRAME_OVERHEAD = 42


def pack_packet(ptype, test_id, seq, size):
    """Build one datagram of exactly `size` payload bytes."""
    if ptype not in (TYPE_DATA, TYPE_END):
        raise ValueError(f"unknown packet type {ptype}")
    if not HEADER_SIZE <= size <= MAX_PAYLOAD:
        raise ValueError(f"size must be between {HEADER_SIZE} and {MAX_PAYLOAD}, got {size}")
    header = HEADER.pack(MAGIC, ptype, 0, test_id, seq, time.time_ns())
    return header + bytes(size - HEADER_SIZE)


def unpack_header(data):
    """Return (ptype, test_id, seq, send_ts_ns). Raise ValueError if invalid."""
    if len(data) < HEADER_SIZE:
        raise ValueError(f"packet too short ({len(data)} bytes)")
    magic, ptype, _reserved, test_id, seq, send_ts_ns = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError(f"bad magic 0x{magic:02x}")
    if ptype not in (TYPE_DATA, TYPE_END):
        raise ValueError(f"unknown packet type {ptype}")
    return ptype, test_id, seq, send_ts_ns

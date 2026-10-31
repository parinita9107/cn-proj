#!/usr/bin/env python3
"""UDP receiver (Member A).

Counts packets and bytes, detects loss, duplicates and out-of-order packets,
measures one-way delay and jitter, logs once per second to receiver_<id>.csv,
and reports the result to the controller (test_end) when the test finishes.

Finishes when an END packet arrives, or after --idle-timeout seconds without
packets once traffic has started.

Example:
    python3 app/receiver.py --port 5001 --test-id 1
"""
import argparse
import csv
import json
import logging
import os
import signal
import socket
import sys
import time

import ctrl_client
from packet import TYPE_DATA, TYPE_END, unpack_header

log = logging.getLogger("receiver")

LINGER_AFTER_END_S = 0.3   # keep reading briefly so late DATA packets are still counted

CSV_FIELDS = ["time_s", "test_id", "rx_pkts", "rx_bytes", "interval_pkts", "interval_bytes",
              "interval_mbps", "max_seq", "expected_pkts", "lost_pkts", "loss_pct",
              "duplicates", "out_of_order", "avg_delay_ms", "jitter_ms"]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="UDP bandwidth meter - receiver")
    ap.add_argument("--bind", default="0.0.0.0", help="address to bind (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=5001, help="UDP port (default 5001)")
    ap.add_argument("--test-id", type=int, default=1, help="only count packets of this test (default 1)")
    ap.add_argument("--idle-timeout", type=float, default=3.0,
                    help="seconds without packets (after traffic started) that end the test (default 3)")
    ap.add_argument("--start-timeout", type=float, default=120.0,
                    help="give up if no packet ever arrives within this many seconds (default 120)")
    ap.add_argument("--log-dir", default=".", help="directory for receiver_<id>.csv (default .)")
    ap.add_argument("--ctrl-url", default=None, help="controller REST URL (default $CTRL_URL or http://127.0.0.1:8080)")
    ap.add_argument("--no-ctrl", action="store_true", help="do not contact the controller")
    args = ap.parse_args(argv)
    if not 1 <= args.port <= 65535:
        ap.error("--port must be 1-65535")
    if args.idle_timeout <= 0 or args.start_timeout <= 0:
        ap.error("timeouts must be greater than 0")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    try:
        sock.bind((args.bind, args.port))
    except OSError as exc:
        sys.exit(f"cannot bind {args.bind}:{args.port}: {exc} (is another receiver still running?)")
    sock.settimeout(0.25)

    os.makedirs(args.log_dir, exist_ok=True)
    csv_file = open(os.path.join(args.log_dir, f"receiver_{args.test_id}.csv"), "w", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    writer.writeheader()

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    seen = bytearray()                 # seen[seq] == 1 once DATA packet seq has arrived
    rx_pkts = rx_bytes = duplicates = out_of_order = invalid = other_test = 0
    max_seq = -1
    end_total = None                   # total DATA packets the sender reports in its END packet
    end_seen_at = None
    delay_sum_ms = 0.0
    delay_n = 0
    prev_transit = None
    jitter_ms = 0.0                    # RFC 3550 style smoothed jitter
    first_t = last_t = None
    last_pkts = last_bytes = 0
    start = time.perf_counter()
    next_log = start + 1.0
    last_log_t = start
    log.info("listening on %s:%d for test %d", args.bind, args.port, args.test_id)

    def expected():
        e = max_seq + 1
        if end_total is not None:
            e = max(e, end_total)
        return e

    def log_row(now):
        nonlocal last_pkts, last_bytes, last_log_t, delay_sum_ms, delay_n
        window = max(now - last_log_t, 1e-9)
        last_log_t = now
        exp = expected()
        lost = max(exp - rx_pkts, 0)
        writer.writerow({
            "time_s": round(now - start, 3), "test_id": args.test_id,
            "rx_pkts": rx_pkts, "rx_bytes": rx_bytes,
            "interval_pkts": rx_pkts - last_pkts, "interval_bytes": rx_bytes - last_bytes,
            "interval_mbps": round((rx_bytes - last_bytes) * 8 / window / 1e6, 4),
            "max_seq": max_seq, "expected_pkts": exp, "lost_pkts": lost,
            "loss_pct": round(100.0 * lost / exp, 4) if exp else 0.0,
            "duplicates": duplicates, "out_of_order": out_of_order,
            "avg_delay_ms": round(delay_sum_ms / delay_n, 4) if delay_n else "",
            "jitter_ms": round(jitter_ms, 4),
        })
        csv_file.flush()
        last_pkts, last_bytes = rx_pkts, rx_bytes
        delay_sum_ms, delay_n = 0.0, 0

    try:
        while not stop["flag"]:
            now_pc = time.perf_counter()
            if now_pc >= next_log:
                log_row(now_pc)
                next_log += 1.0
            if end_seen_at is not None and now_pc - end_seen_at > LINGER_AFTER_END_S:
                log.info("END packet received, finishing")
                break
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                if first_t is None and now_pc - start > args.start_timeout:
                    log.error("no packets within %.0f s, giving up", args.start_timeout)
                    break
                if last_t is not None and time.time() - last_t > args.idle_timeout:
                    log.info("idle for %.1f s, finishing", args.idle_timeout)
                    break
                continue
            except OSError as exc:
                log.warning("receive error: %s", exc)
                continue

            recv_ns = time.time_ns()
            try:
                ptype, test_id, seq, send_ts_ns = unpack_header(data)
            except ValueError as exc:
                invalid += 1
                if invalid <= 3:
                    log.warning("ignored invalid packet (%s)", exc)
                continue
            if test_id != args.test_id:
                other_test += 1
                continue

            if first_t is None:
                first_t = time.time()
                log.info("first packet received")
            last_t = time.time()

            if ptype == TYPE_END:
                end_total = seq if end_total is None else max(end_total, seq)
                if end_seen_at is None:
                    end_seen_at = time.perf_counter()
                continue

            # DATA packet
            if seq < len(seen) and seen[seq]:
                duplicates += 1
                continue
            if seq >= len(seen):
                seen.extend(bytes(seq + 1 - len(seen) + 4096))
            seen[seq] = 1
            if seq < max_seq:
                out_of_order += 1
            max_seq = max(max_seq, seq)
            rx_pkts += 1
            rx_bytes += len(data)

            transit_ms = (recv_ns - send_ts_ns) / 1e6
            delay_sum_ms += transit_ms
            delay_n += 1
            if prev_transit is not None:
                jitter_ms += (abs(transit_ms - prev_transit) - jitter_ms) / 16.0
            prev_transit = transit_ms
    finally:
        log_row(time.perf_counter())
        csv_file.close()
        sock.close()

    exp = expected()
    lost = max(exp - rx_pkts, 0)
    loss_pct = round(100.0 * lost / exp, 4) if exp else 0.0
    duration = (last_t - first_t) if (first_t and last_t and last_t > first_t) else 0.0
    summary = {
        "role": "receiver", "test_id": args.test_id, "rx_pkts": rx_pkts, "rx_bytes": rx_bytes,
        "expected_pkts": exp, "lost_pkts": lost, "loss_pct": loss_pct,
        "duplicates": duplicates, "out_of_order": out_of_order, "invalid_packets": invalid,
        "other_test_packets": other_test, "end_received": end_total is not None,
        "duration_s": round(duration, 3),
        "throughput_mbps": round(rx_bytes * 8 / duration / 1e6, 4) if duration else 0.0,
        "jitter_ms": round(jitter_ms, 4),
        "interrupted": stop["flag"],
    }
    log.info("done: received %d of %d packets (%.2f%% loss), %d dup, %d out of order, %.3f Mbit/s",
             rx_pkts, exp, loss_pct, duplicates, out_of_order, summary["throughput_mbps"])

    if first_t is not None and not args.no_ctrl:
        ctrl_client.send_test_end(args.test_id, rx_pkts, rx_bytes, exp, lost, loss_pct,
                                  summary["duration_s"], args.ctrl_url)
    print("SUMMARY " + json.dumps(summary))
    return 0 if first_t is not None else 1


if __name__ == "__main__":
    sys.exit(main())

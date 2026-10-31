#!/usr/bin/env python3
"""UDP traffic generator (Member A).

Sends DATA packets of a fixed payload size at a fixed rate for a fixed time,
then 3 END packets 50 ms apart. Logs once per second to sender_<id>.csv.

Example:
    python3 app/sender.py --dst 10.0.0.2 --port 5001 --rate 5 --size 1000 \
        --duration 20 --test-id 1
"""
import argparse
import csv
import ipaddress
import json
import logging
import os
import random
import signal
import socket
import sys
import time

import ctrl_client
from packet import (HEADER_SIZE, MAX_PAYLOAD, TYPE_DATA, TYPE_END, pack_packet)

log = logging.getLogger("sender")

END_COUNT = 3
END_GAP_S = 0.05
MAX_SEND_ERRORS = 50   # ICMP errors arrive on every other send, so count the total

CSV_FIELDS = ["time_s", "test_id", "tx_pkts", "tx_bytes", "interval_pkts",
              "interval_bytes", "interval_mbps", "target_mbps", "dropped_pkts", "errors"]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="UDP bandwidth meter - sender")
    ap.add_argument("--dst", required=True, help="receiver IP address")
    ap.add_argument("--port", type=int, default=5001, help="receiver UDP port (default 5001)")
    ap.add_argument("--rate", type=float, required=True, help="offered load in Mbit/s (payload bytes)")
    ap.add_argument("--size", type=int, default=1000,
                    help=f"UDP payload bytes, {HEADER_SIZE} to {MAX_PAYLOAD} (default 1000)")
    ap.add_argument("--duration", type=float, default=10, help="seconds of DATA traffic (default 10)")
    ap.add_argument("--test-id", type=int, default=1, help="test identifier (default 1)")
    ap.add_argument("--log-dir", default=".", help="directory for sender_<id>.csv (default .)")
    ap.add_argument("--ctrl-url", default=None, help="controller REST URL (default $CTRL_URL or http://127.0.0.1:8080)")
    ap.add_argument("--no-ctrl", action="store_true", help="do not contact the controller")
    ap.add_argument("--drop-prob", type=float, default=0.0,
                    help="TEST MODE: probability (0-1) of deliberately not sending a packet, to check loss detection")
    ap.add_argument("--seed", type=int, default=None, help="random seed for --drop-prob")
    args = ap.parse_args(argv)

    problems = []
    try:
        ipaddress.ip_address(args.dst)
    except ValueError:
        problems.append(f"--dst '{args.dst}' is not a valid IP address")
    if not 1 <= args.port <= 65535:
        problems.append("--port must be 1-65535")
    if args.rate <= 0:
        problems.append("--rate must be greater than 0")
    if not HEADER_SIZE <= args.size <= MAX_PAYLOAD:
        problems.append(f"--size must be {HEADER_SIZE}-{MAX_PAYLOAD}")
    if args.duration <= 0:
        problems.append("--duration must be greater than 0")
    if not 0 <= args.drop_prob < 1:
        problems.append("--drop-prob must be in [0, 1)")
    if problems:
        ap.error("; ".join(problems))
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rng = random.Random(args.seed)

    interval = args.size * 8 / (args.rate * 1e6)   # seconds between packets
    total_planned = int(args.duration / interval)
    if total_planned < 1:
        sys.exit("rate and duration are too small to send even one packet")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # connect() on UDP lets the kernel report "port unreachable" (receiver not running)
        sock.connect((args.dst, args.port))
        src_ip = sock.getsockname()[0]
    except OSError as exc:
        sys.exit(f"cannot set up socket to {args.dst}:{args.port}: {exc}")

    os.makedirs(args.log_dir, exist_ok=True)
    csv_path = os.path.join(args.log_dir, f"sender_{args.test_id}.csv")
    csv_file = open(csv_path, "w", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    writer.writeheader()

    interrupted = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: interrupted.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: interrupted.update(flag=True))

    if not args.no_ctrl:
        ctrl_client.send_test_start(args.test_id, src_ip, args.dst, args.port, args.rate,
                                    args.size, args.duration, args.ctrl_url)

    log.info("test %d: %.2f Mbit/s, %d B payload, %.1f s -> %s:%d (%d packets planned)",
             args.test_id, args.rate, args.size, args.duration, args.dst, args.port, total_planned)

    tx_pkts = tx_bytes = dropped = errors = 0
    warned = set()
    last_pkts = last_bytes = 0
    aborted = None
    t0 = time.perf_counter()
    next_log = t0 + 1.0

    last_t = t0

    def log_row(now, final=False):
        nonlocal last_pkts, last_bytes, last_t
        elapsed = now - t0
        d_pkts, d_bytes = tx_pkts - last_pkts, tx_bytes - last_bytes
        window = max(now - last_t, 1e-9)   # real length of this logging interval
        last_t = now
        writer.writerow({
            "time_s": round(elapsed, 3), "test_id": args.test_id,
            "tx_pkts": tx_pkts, "tx_bytes": tx_bytes,
            "interval_pkts": d_pkts, "interval_bytes": d_bytes,
            "interval_mbps": round(d_bytes * 8 / window / 1e6, 4),
            "target_mbps": args.rate, "dropped_pkts": dropped, "errors": errors,
        })
        csv_file.flush()
        last_pkts, last_bytes = tx_pkts, tx_bytes

    seq = 0
    try:
        while seq < total_planned and not interrupted["flag"]:
            target = t0 + seq * interval
            now = time.perf_counter()
            wait = target - now
            if wait > 0.002:
                time.sleep(wait - 0.001)       # coarse sleep, then spin for accuracy
            while time.perf_counter() < target:
                pass
            # if we are behind schedule the loop sends back-to-back until caught up

            if args.drop_prob and rng.random() < args.drop_prob:
                dropped += 1                    # sequence number is consumed but nothing is sent
            else:
                try:
                    sock.send(pack_packet(TYPE_DATA, args.test_id, seq, args.size))
                    tx_pkts += 1
                    tx_bytes += args.size
                except ConnectionRefusedError:
                    errors += 1
                    if "refused" not in warned:
                        warned.add("refused")
                        log.warning("receiver at %s:%d is not listening (ICMP port unreachable)",
                                    args.dst, args.port)
                except OSError as exc:
                    errors += 1
                    if str(exc) not in warned:
                        warned.add(str(exc))
                        log.warning("send error: %s", exc)
                if errors >= MAX_SEND_ERRORS:
                    aborted = "too many send errors, receiver or network unreachable"
                    break
            seq += 1

            now = time.perf_counter()
            if now >= next_log:
                log_row(now)
                next_log += 1.0
    finally:
        end_time = time.perf_counter()
        # END packets carry the total number of DATA sequence numbers used, so the
        # receiver can count packets lost at the very end of the run too.
        for i in range(END_COUNT):
            try:
                sock.send(pack_packet(TYPE_END, args.test_id, seq, HEADER_SIZE))
            except OSError:
                pass
            if i < END_COUNT - 1:
                time.sleep(END_GAP_S)
        log_row(end_time, final=True)
        csv_file.close()
        sock.close()

    elapsed = max(end_time - t0, 1e-9)
    summary = {
        "role": "sender", "test_id": args.test_id, "tx_pkts": tx_pkts, "tx_bytes": tx_bytes,
        "seq_used": seq, "deliberately_dropped": dropped, "send_errors": errors,
        "duration_s": round(elapsed, 3),
        "achieved_mbps": round(tx_bytes * 8 / elapsed / 1e6, 4),
        "target_mbps": args.rate, "interrupted": interrupted["flag"], "aborted": aborted,
    }
    if aborted:
        log.error(aborted)
    log.info("done: sent %d packets (%d bytes) in %.2f s = %.3f Mbit/s (target %.3f)",
             tx_pkts, tx_bytes, elapsed, summary["achieved_mbps"], args.rate)
    if summary["achieved_mbps"] < 0.95 * args.rate and not interrupted["flag"] and not aborted:
        log.warning("achieved rate is more than 5%% below the target; Python cannot keep up "
                    "at this rate/size, use a larger --size or a lower --rate")
    print("SUMMARY " + json.dumps(summary))
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())

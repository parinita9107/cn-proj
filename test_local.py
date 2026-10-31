#!/usr/bin/env python3
"""Local test of sender and receiver on one machine, no Mininet, no controller.

Run from the repository root:   python3 app/test_local.py

Case 1: nothing dropped  -> receiver counts must equal sender counts, 0 loss.
Case 2: sender deliberately drops packets -> receiver must detect exactly that many as lost.
Case 3: bad arguments and missing receiver are handled without a crash.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 5701


def run_pair(extra_sender_args, test_id):
    with tempfile.TemporaryDirectory() as logs:
        rx = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "receiver.py"), "--bind", "127.0.0.1",
             "--port", str(PORT), "--test-id", str(test_id), "--no-ctrl", "--log-dir", logs],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(0.6)
        tx = subprocess.run(
            [sys.executable, os.path.join(HERE, "sender.py"), "--dst", "127.0.0.1",
             "--port", str(PORT), "--test-id", str(test_id), "--no-ctrl", "--log-dir", logs]
            + extra_sender_args, capture_output=True, text=True, timeout=60)
        rx_out, _ = rx.communicate(timeout=30)
        tx_sum = json.loads([l for l in tx.stdout.splitlines() if l.startswith("SUMMARY")][-1][8:])
        rx_sum = json.loads([l for l in rx_out.splitlines() if l.startswith("SUMMARY")][-1][8:])
        for name in (f"sender_{test_id}.csv", f"receiver_{test_id}.csv"):
            assert os.path.getsize(os.path.join(logs, name)) > 0, f"{name} empty"
    return tx_sum, rx_sum


def main():
    # Case 1
    tx, rx = run_pair(["--rate", "5", "--size", "1000", "--duration", "3"], 1)
    assert tx["tx_pkts"] == rx["rx_pkts"], (tx, rx)
    assert tx["tx_bytes"] == rx["rx_bytes"], (tx, rx)
    assert rx["lost_pkts"] == 0 and rx["duplicates"] == 0, rx
    assert rx["end_received"], rx
    print(f"PASS case 1: sent {tx['tx_pkts']} received {rx['rx_pkts']} lost 0, "
          f"achieved {tx['achieved_mbps']} Mbit/s (target 5)")

    # Case 2
    tx, rx = run_pair(["--rate", "5", "--size", "1000", "--duration", "3",
                       "--drop-prob", "0.1", "--seed", "7"], 2)
    assert tx["deliberately_dropped"] > 0, tx
    assert rx["lost_pkts"] == tx["deliberately_dropped"], (tx, rx)
    assert rx["rx_pkts"] == tx["tx_pkts"], (tx, rx)
    print(f"PASS case 2: sender dropped {tx['deliberately_dropped']}, "
          f"receiver detected {rx['lost_pkts']} lost ({rx['loss_pct']}%)")

    # Case 3
    bad = subprocess.run([sys.executable, os.path.join(HERE, "sender.py"), "--dst", "not-an-ip",
                          "--rate", "5", "--no-ctrl"], capture_output=True, text=True)
    assert bad.returncode != 0 and "Traceback" not in bad.stderr, bad.stderr
    gone = subprocess.run([sys.executable, os.path.join(HERE, "sender.py"), "--dst", "127.0.0.1",
                           "--port", "5799", "--rate", "5", "--duration", "2", "--no-ctrl",
                           "--log-dir", tempfile.mkdtemp()], capture_output=True, text=True, timeout=30)
    assert "Traceback" not in gone.stderr, gone.stderr
    assert gone.returncode == 1 and "not listening" in gone.stderr, gone.stderr
    print("PASS case 3: bad IP rejected cleanly, missing receiver detected, no crash")

    # Case 4: controller unreachable must only warn
    gone = subprocess.run([sys.executable, os.path.join(HERE, "sender.py"), "--dst", "127.0.0.1",
                           "--port", "5799", "--rate", "5", "--duration", "1",
                           "--ctrl-url", "http://127.0.0.1:1", "--log-dir", tempfile.mkdtemp()],
                          capture_output=True, text=True, timeout=30)
    assert "controller not reachable" in gone.stderr and "Traceback" not in gone.stderr, gone.stderr
    print("PASS case 4: unreachable controller only logs a warning")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()

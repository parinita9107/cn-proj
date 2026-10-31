"""Control-plane client: tells the Ryu controller when a test starts and ends.

Sends HTTP POST with a JSON body to  <ctrl_url>/test.
If the controller cannot be reached, a warning is logged and the application
carries on (the measurement must never depend on the controller being up).

Controller URL: argument, else environment variable CTRL_URL, else
http://127.0.0.1:8080  (how hosts reach the controller is still an open item
in the team plan, so it is configurable).

Message formats (proposed for docs/INTERFACES.md):

  test_start  {"event": "test_start", "test_id": 1, "src_ip": "10.0.0.1",
               "dst_ip": "10.0.0.2", "udp_port": 5001, "target_rate_mbps": 5.0,
               "payload_size": 1000, "duration_s": 20.0, "ts": 1730000000.0}

  test_end    {"event": "test_end", "test_id": 1, "rx_pkts": 12500,
               "rx_bytes": 12500000, "expected_pkts": 12500, "lost_pkts": 0,
               "loss_pct": 0.0, "duration_s": 20.0, "ts": 1730000020.0}
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger("ctrl_client")

DEFAULT_URL = "http://127.0.0.1:8080"
TIMEOUT_S = 2.0


def _post(ctrl_url, payload):
    url = (ctrl_url or os.environ.get("CTRL_URL") or DEFAULT_URL).rstrip("/") + "/test"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            log.info("controller accepted %s (HTTP %s)", payload["event"], resp.status)
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("controller not reachable at %s (%s); continuing without it", url, exc)
        return False


def send_test_start(test_id, src_ip, dst_ip, udp_port, target_rate_mbps,
                    payload_size, duration_s, ctrl_url=None):
    return _post(ctrl_url, {
        "event": "test_start",
        "test_id": test_id,
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "udp_port": udp_port,
        "target_rate_mbps": target_rate_mbps,
        "payload_size": payload_size,
        "duration_s": duration_s,
        "ts": time.time(),
    })


def send_test_end(test_id, rx_pkts, rx_bytes, expected_pkts, lost_pkts,
                  loss_pct, duration_s, ctrl_url=None):
    return _post(ctrl_url, {
        "event": "test_end",
        "test_id": test_id,
        "rx_pkts": rx_pkts,
        "rx_bytes": rx_bytes,
        "expected_pkts": expected_pkts,
        "lost_pkts": lost_pkts,
        "loss_pct": loss_pct,
        "duration_s": duration_s,
        "ts": time.time(),
    })

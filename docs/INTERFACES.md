# Interfaces

The fixed contract between the UDP application (`app/`), the controller (`sdn/`) and
the experiment and analysis scripts (`experiments/`).

## 1. Addresses and ports

| Node | Address | Role |
|---|---|---|
| h1 | 10.0.0.1 | Measurement sender |
| h2 | 10.0.0.2 | Receiver (measurement and background) |
| h3 | 10.0.0.3 | Background sender (congestion scenario) |
| h4, h5 | 10.0.0.4, 10.0.0.5 | Extra hosts |
| nat0 | 10.0.0.254 | Route from hosts to the controller REST API |
| Ryu | 127.0.0.1:6653 (OpenFlow 1.3), 0.0.0.0:8080 (REST) | Controller |

| Port | Use |
|---|---|
| UDP 5001 | Measurement flow |
| UDP 5002 | Background flow |
| TCP 8080 | Controller REST API |
| TCP 6653 | OpenFlow |

## 2. UDP packet format

20-byte header in network byte order (`struct` format `!BBHIIQ`), followed by zero
padding up to the requested payload size (20 to 1472 bytes).

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 1 | `magic` | `0xB7`. Packets with any other value are rejected |
| 1 | 1 | `type` | 1 = DATA, 2 = END |
| 2 | 2 | `reserved` | Always 0 |
| 4 | 4 | `test_id` | Test run identifier. The receiver ignores other test ids |
| 8 | 4 | `seq` | DATA: sequence number from 0. END: total DATA sequence numbers used |
| 12 | 8 | `send_ts_ns` | Sender clock (`time.time_ns()`) when the packet was built |

The sender sends 3 END packets 50 ms apart. The receiver finishes on the first END
(after a 0.3 s linger for late packets) or after 3 s without packets.

## 3. Controller REST messages

`POST http://10.0.0.254:8080/test` with a JSON body. Response: `{"ok": true}`.
If the controller is unreachable, the app logs a warning and continues.

**test_start** (sender, before the first packet):

```json
{"event": "test_start", "test_id": 1,
 "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2", "udp_port": 5001,
 "src": "10.0.0.1", "dst": "10.0.0.2", "dst_port": 5001,
 "target_rate_mbps": 5.0, "payload_size": 1000, "duration_s": 15.0,
 "ts": 1794505891.78}
```

`src`, `dst` and `dst_port` are the names the controller uses to match the flow.
`target_rate_mbps` is the rate the flow declares. The controller enforces it in
dynamic mode.

**test_end** (receiver, after the test):

```json
{"event": "test_end", "test_id": 1, "rx_pkts": 10507, "rx_bytes": 10507000,
 "expected_pkts": 18750, "lost_pkts": 8243, "loss_pct": 43.96,
 "duration_s": 14.98, "ts": 1794505907.15}
```

## 4. Controller behaviour

| Mode | Forwarding | Metering |
|---|---|---|
| `static` (baseline) | Learning switch. One priority-20 rule per UDP flow (src, dst, UDP dst port); priority-10 rules per MAC pair and packet type for other traffic | None |
| `dynamic` | Every UDP flow rule points to a meter, which starts at 1 Gbps (no limit) | Declared target converted to frame bytes: target × (payload + 42) / payload. If the measured rate is above 1.2 × that for 3 polls in a row: meter set to it (`rate_limit_on`). After 5 polls below 0.5 × target: limit removed (`rate_limit_off`) |

**Blocking (dynamic mode only).** UDP ports 5001 and 5002 are reserved for
registered tests (`PROTECTED_PORTS`). A packet to one of them whose flow (src, dst,
port) has no `test_start` is dropped, and a priority-30 drop rule is installed
(`blocked` event). The rule expires 15 s after the traffic stops (`block_removed`,
with the number of packets dropped). If that sender later sends `test_start`, the
drop rule is deleted at once (`unblocked`).

Mode is set with the `CONTROLLER_MODE` environment variable. Logs go to `RESULTS_DIR`.

## 5. Log files (CSV)

**`sender_<id>.csv`** (once per second, plus a final row)

`time_s, test_id, tx_pkts, tx_bytes, interval_pkts, interval_bytes, interval_mbps, target_mbps, dropped_pkts, errors, blocked`

`blocked` counts ENOBUFS waits: the outgoing queue was full because the network
could not carry the offered rate, so the sender paused and retried the same packet
(back-pressure). The sender's achieved rate then drops below the target.

**`receiver_<id>.csv`** (once per second, plus a final row)

`time_s, test_id, rx_pkts, rx_bytes, interval_pkts, interval_bytes, interval_mbps, max_seq, expected_pkts, lost_pkts, loss_pct, duplicates, out_of_order, avg_delay_ms, jitter_ms`

Byte counts are UDP payload bytes. `jitter_ms` is the RFC 3550 smoothed jitter.

**`flowstats.csv`** (controller, one row per UDP flow rule per switch per poll)

`ts, dpid, flow_id, pkts, bytes, duration_s, delta_pkts, delta_bytes, rate_mbps`

`flow_id` is `src>dst:udp_port`. Bytes are Ethernet frame bytes (payload + 42).

**`controller_events.csv`**

`ts, event, flow_id, detail`

Events: `test_start`, `test_end`, `rate_limit_on`, `rate_limit_off`,
`flow_removed` (with the rule's final packet and byte counts), `blocked`,
`unblocked`, `block_removed` (with the number of packets dropped).

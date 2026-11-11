# Project 26: Bandwidth Meter and Traffic Analyzer
## Combined Project README

This README combines the project-wide setup and architecture documentation with the Member A application documentation and the Member C experiment-analysis documentation. The D1 and D2 measurements below are the results from our recorded runs.

---

## 1. Project overview

Project 26 implements a UDP bandwidth meter and traffic analyzer using Mininet, Open vSwitch and the Ryu SDN controller. The application generates UDP traffic, measures what reaches the receiver, and reports test events to the controller. The controller polls per-flow statistics and, in dynamic mode, can apply rate limiting to a flow.

## 2. Project layout

| Path | Purpose |
|---|---|
| `app/sender.py` | UDP traffic generator with configurable rate and payload size |
| `app/receiver.py` | UDP receiver measuring throughput, loss, duplicates, reordering, delay and jitter |
| `app/packet.py` | Packet format shared by sender and receiver |
| `app/ctrl_client.py` | Sends test start/end events to the controller |
| `app/test_local.py` | Local sender/receiver tests without Mininet or the controller |
| `sdn/controller.py` / `controller.py` | Ryu SDN controller, depending on repository layout |
| `sdn/topology.py` / `topology.py` | Mininet network topology, depending on repository layout |
| `experiments/` | Experiment configurations, runners and analysis scripts |
| `results/` | CSV logs, comparison tables and generated graphs |

## 3. Network topology and environment

The uploaded project README describes a Mininet topology with hosts h1 to h5, switches s1 to s3, and a NAT/controller REST path. It lists the controller REST address as `10.0.0.254:8080`.

Our recorded D1 and D2 runs used the project setup with the controller REST base URL `http://10.0.0.4:8080`. Use the address that matches the topology and controller configuration currently being run. Do not mix the two addresses.

### Main components

- Ubuntu with Mininet
- Open vSwitch using OpenFlow 1.3
- Ryu controller (version 4.34 in our recorded setup)
- Python 3
- UDP sender and receiver applications

### Basic run sequence

1. Open the project directory.
2. Clean up old Mininet state with `sudo mn -c`.
3. Start the Ryu controller in static or dynamic mode.
4. Start the Mininet topology.
5. Run `pingall` and confirm that the hosts can communicate.
6. Start the receiver before starting the sender.
7. After the experiment, exit Mininet and clean up with `sudo mn -c`.

Example controller modes:

```bash
# Static mode
ryu-manager sdn/controller.py

# Dynamic mode
CONTROLLER_MODE=dynamic ryu-manager sdn/controller.py
```

The exact controller and topology file paths depend on the repository layout. In our working project directory, the path was:

```bash
cd /media/sf_cn-proj-main
```

Activate the Ryu virtual environment before running the controller:

```bash
source ~/ryu-env/bin/activate
```

## 4. UDP application (Member A)

The UDP application generates traffic at a configured rate on one Mininet host and measures what arrives at another host. The sender and receiver can report test start and end events to the Ryu controller through its REST API.

### 4.1 Packet format

The packet format is shared by the sender and receiver. The uploaded application documentation describes a 20-byte header using the Python struct format `!BBHIIQ`:

| Field | Bytes | Meaning |
|---|---:|---|
| `magic` | 1 | `0xB7`; identifies valid application packets |
| `type` | 1 | `1` = DATA, `2` = END |
| `reserved` | 2 | Reserved field |
| `test_id` | 4 | Identifies the experiment run |
| `seq` | 4 | DATA sequence number, or total DATA packets in END |
| `send_ts_ns` | 8 | Sender timestamp in nanoseconds |

The rest of the payload is padded to the requested payload size. The documented payload range is 20 to 1472 bytes.

For our recorded 1000-byte payload experiments, the switch counts approximately 1042 bytes per packet after adding the UDP (8 bytes), IP (20 bytes) and Ethernet (14 bytes) headers. Therefore, switch-side byte counts are expected to be about 1.042 times the application payload byte count.

### 4.2 Sender

The sender generates UDP traffic at the configured application rate. It records transmitted packet and byte counts and can send test start/end information to the controller.

### 4.3 Receiver

The receiver records the traffic that arrives from the sender. The application supports measurements including received packets, packet loss, received bytes, interval throughput, duplicates, out-of-order packets, delay and jitter.

### 4.4 Controller communication

The application reports test information to the Ryu controller through its REST API. The controller receives test details such as the test ID, source and destination addresses, UDP port, target rate, payload size and duration.

For our recorded D1 and D2 runs, the correct controller REST base URL was:

```text
http://10.0.0.4:8080
```

Use the base URL with `--ctrl-url`. The application appends `/test` itself, so do not append `/test` to the argument.

### 4.5 Application log files

| File | Information recorded |
|---|---|
| `sender_<id>.csv` | Transmission time, test ID, transmitted packets/bytes, interval throughput, target rate, dropped packets and errors |
| `receiver_<id>.csv` | Receive time, test ID, received packets/bytes, interval throughput, expected/lost packets, loss percentage, duplicates, reordering, delay and jitter |
| `controller_events.csv` | Controller test and rate-limit events |
| `flowstats.csv` | Per-flow statistics collected by the controller |

The exact columns depend on the application version. In the documented CSV format, `tx_pkts`, `rx_pkts` and byte counts are cumulative, while `interval_*` columns describe the latest logging interval.

---

## 5. Experiments and results (Member C)

Member C's work covers running and evaluating the D1 and D2 experiments, collecting outputs, analysing measurements, generating graphs and comparing results.

### 5.1 D1: Static baseline experiment

D1 measures network behaviour with the controller in static mode. The sender offers 5 Mbps for 20 seconds using 1000-byte payloads. Dynamic rate limiting is not applied.

#### D1 configuration

| Parameter | Value |
|---|---:|
| Controller mode | Static |
| Source host | h1 |
| Destination host | h2 |
| Destination IP | `10.0.0.2` |
| UDP port | 5001 |
| Offered application rate | 5 Mbps |
| Payload size | 1000 bytes |
| Duration | 20 seconds |
| Test ID | 1 |

#### D1 recorded results

| Measurement | Result |
|---|---:|
| Packets sent | 12,500 |
| Packets received | 12,500 |
| Packets lost | 0 |
| Packet loss | 0% |
| Application payload sent | 12,500,000 bytes |
| Switch-side bytes | 13,025,368 bytes |
| Switch/application byte ratio | 1.042029 |

All packets sent by the application were received. The switch counted more bytes because its counters include Ethernet, IP and UDP headers. The measured ratio of approximately 1.042 agrees with the expected overhead for a 1000-byte payload.

**D1 finding:** The static experiment measured the baseline traffic successfully, with 0% packet loss.

### 5.2 D2: Dynamic bandwidth-control experiment

D2 tests the controller's dynamic response to traffic above a configured threshold. The sender offers 10 Mbps. The controller uses a 6 Mbps trigger threshold and installs an OpenFlow meter configured at 5000 kbps (5 Mbps) after the measured flow exceeds the threshold.

#### D2 configuration

| Parameter | Value |
|---|---:|
| Controller mode | Dynamic |
| Source host | h1 |
| Destination host | h2 |
| Destination IP | `10.0.0.2` |
| UDP port | 5001 |
| Offered application rate | 10 Mbps |
| Trigger threshold | 6 Mbps |
| Meter rate | 5000 kbps (5 Mbps) |
| Payload size | 1000 bytes |
| Duration | 20 seconds |
| Test ID | 2 |

#### D2 recorded results

| Measurement | Result |
|---|---:|
| Packets sent | 25,000 |
| Packets received | 13,527 |
| Packets lost | 11,473 |
| Packet loss | 45.892% |
| Application payload sent | 25,000,000 bytes |
| Application payload received | 13,527,000 bytes |
| Receiver mean interval throughput | Approximately 5.15 Mbps |

The controller logged the `rate_limit_on` event. The switch flow table showed the measured UDP flow attached to meter 1, and the switch meter table showed the configured 5000 kbps drop band.

**D2 finding:** The dynamic rate-limiting mechanism was activated. The receiver measured approximately 5.15 Mbps mean interval throughput while the sender offered 10 Mbps. The observed packet loss was 45.892%. This is the measured outcome of the run, not proof of perfectly exact 5 Mbps enforcement.

### 5.3 D1 and D2 comparison

| Metric | D1: Static | D2: Dynamic |
|---|---:|---:|
| Offered sender rate | 5 Mbps | 10 Mbps |
| Controller mode | Static | Dynamic |
| Trigger threshold | Not used | 6 Mbps |
| Meter rate | Not applied | 5 Mbps |
| Packets sent | 12,500 | 25,000 |
| Packets received | 12,500 | 13,527 |
| Packets lost | 0 | 11,473 |
| Packet loss | 0% | 45.892% |

**Interpretation:**

- D1 measures baseline network behaviour at a 5 Mbps offered load. Every transmitted packet was received.
- D2 offers twice the D1 sender rate. When the controller detects traffic above its 6 Mbps threshold, it installs a 5 Mbps meter on the matching UDP flow.
- D2's receiver throughput was approximately 5.15 Mbps, with packet loss observed after rate limiting was applied.

The key difference is that D1 observes traffic without dynamically limiting it, whereas D2 monitors traffic and reacts by applying a meter.

### 5.4 Result directories and generated analysis files

For our recorded runs:

| Experiment | Results directory |
|---|---|
| D1 | `results/D1_LIVE/` |
| D2 | `results/D2_LIVE/` |

The D2 analysis outputs were generated in:

```text
results/D2_LIVE/analysis/
```

Generated D2 files:

- `D2_sender_receiver.png`
- `D2_packet_loss.png`
- `D2_throughput_comparison.png`
- `D2_comparison_table.csv`

These outputs present sender and receiver throughput, packet loss and the measured performance of the dynamic experiment.

---

## 6. Additional experiment matrix from the original project README

The uploaded project-wide README also documents the following planned or separate test cases. These are kept separate from the D1 and D2 results above because they use different test IDs, rates, durations or run conditions.

| Test | Flow | Rate | Payload | Duration | Controller | Purpose/status in source README |
|---|---|---:|---:|---:|---|---|
| 11 | h1 → h2:5001 | 5 Mbps | 1000 B | 20 s | TODO | Baseline |
| 12 | h3 → h2:5002 | 5 Mbps | 500 B | 20 s | TODO | Baseline with smaller packets |
| 31 | h1 → h2:5001 | 10 Mbps | 1400 B | 60 s | Static | Multi-flow, large packets |
| 32 | h3 → h2:5002 | 2 Mbps | 200 B | 60 s | Static | Multi-flow, small packets |
| 33 | h1 → h2:5033 | 3 Mbps | 500 B | 60 s | Static | Medium packets; source README says rerun on Nov 11 |
| 41 | TODO | 10 Mbps | 1000 B | 30 s | TODO | Purpose not specified |
| 42 | h1 → h2:5001, declared 5 Mbps and sending 10 Mbps | 10 Mbps | 1000 B | 30 s | Dynamic | Rate-limit integration |

The original README gives expected outcomes for some of these tests, including approximately 50% loss for test 42 and near-zero loss for tests 31 and 32. Those are expectations or separate test records, not the measured D1/D2 results reported in Section 5.

---

## 7. Troubleshooting notes

The original project README lists these common issues:

- **Interface pair already exists:** run `sudo mn -c` and retry.
- **`pingall` fails or the receiver gets zero packets:** check that Ryu is running before starting the topology and verify that the switches are connected to the controller.
- **Controller unreachable at `127.0.0.1`:** use the controller REST address reachable from the Mininet hosts. In our recorded D1/D2 setup this was `10.0.0.4:8080`; the uploaded topology README describes `10.0.0.254:8080`.

---

## 8. Overall conclusion

Project 26 combines UDP traffic generation and measurement with SDN-based monitoring and dynamic bandwidth control.

In our recorded D1 run, all 12,500 transmitted packets were received, giving 0% packet loss. In D2, the sender offered 10 Mbps and the dynamic controller activated its configured 5 Mbps OpenFlow meter after the measured flow crossed the 6 Mbps trigger threshold. The receiver obtained 13,527 of 25,000 packets, with 45.892% packet loss and approximately 5.15 Mbps mean interval throughput.

Together, the experiments demonstrate the difference between static baseline measurement and controller-driven dynamic rate limiting. The application provides the traffic generation and measurement, while the controller monitors flows and applies the configured dynamic response.

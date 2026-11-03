#!/usr/bin/env python3
"""Analyze Project 26 sender, receiver, OpenFlow and controller CSV logs.

The analyzer deliberately distinguishes cumulative counters from interval deltas.
For network/application comparisons it uses the selected measurement flow on s1
when switch identifiers are available. It never sums the same packets across
multiple switches.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lookup = {norm(col): col for col in df.columns}
    for candidate in candidates:
        if norm(candidate) in lookup:
            return lookup[norm(candidate)]
    return None


def numeric(df: pd.DataFrame, candidates: list[str]) -> pd.Series | None:
    col = find_column(df, candidates)
    return pd.to_numeric(df[col], errors="coerce") if col else None


def read_csvs(root: Path, patterns: list[str]) -> pd.DataFrame:
    paths: set[str] = set()
    for pattern in patterns:
        paths.update(glob.glob(str(root / pattern), recursive=True))
    frames = []
    for filename in sorted(paths):
        try:
            frame = pd.read_csv(filename)
            if not frame.empty:
                frame["_source_file"] = Path(filename).name
                frame["_run_dir"] = Path(filename).parent.name
                frames.append(frame)
        except (OSError, pd.errors.ParserError, UnicodeDecodeError) as exc:
            print(f"WARNING: skipping {filename}: {exc}", file=sys.stderr)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def select_s1_measurement_flow(flows: pd.DataFrame) -> pd.DataFrame:
    """Select s1 rows and, where possible, the h1->h2 UDP/5001 measurement flow."""
    if flows.empty:
        return flows
    result = flows.copy()
    switch_col = find_column(result, ["switch", "switch_name", "dpid", "datapath_id"])
    if switch_col:
        values = result[switch_col].astype(str).str.lower()
        s1 = result[values.isin({"1", "s1", "0000000000000001", "0x1"})]
        if not s1.empty:
            result = s1

    # Narrow only when the CSV actually exposes the relevant fields.
    src_col = find_column(result, ["ipv4_src", "ip_src", "src_ip", "nw_src"])
    dst_col = find_column(result, ["ipv4_dst", "ip_dst", "dst_ip", "nw_dst"])
    port_col = find_column(result, ["udp_dst", "udp_dst_port", "dst_port", "tp_dst"])
    mask = pd.Series(True, index=result.index)
    if src_col:
        mask &= result[src_col].astype(str).eq("10.0.0.1")
    if dst_col:
        mask &= result[dst_col].astype(str).eq("10.0.0.2")
    if port_col:
        mask &= pd.to_numeric(result[port_col], errors="coerce").eq(5001)
    narrowed = result[mask]
    return narrowed if not narrowed.empty else result


def max_numeric(df: pd.DataFrame, candidates: list[str]) -> float | None:
    values = numeric(df, candidates)
    if values is None:
        return None
    values = values.dropna()
    return float(values.max()) if not values.empty else None


def mean_numeric(df: pd.DataFrame, candidates: list[str]) -> float | None:
    values = numeric(df, candidates)
    if values is None:
        return None
    values = values.dropna()
    return float(values.mean()) if not values.empty else None


def plot_rates(out_dir: Path, groups: list[tuple[str, pd.DataFrame]]) -> None:
    plt.figure(figsize=(9, 5))
    plotted = False
    for label, frame in groups:
        if frame.empty:
            continue
        rate_col = find_column(frame, ["interval_mbps", "rate_mbps", "throughput_mbps", "tx_mbps", "rx_mbps", "app_throughput_mbps", "network_throughput_mbps"])
        if not rate_col:
            continue
        values = pd.to_numeric(frame[rate_col], errors="coerce").dropna()
        if values.empty:
            continue
        plt.plot(range(len(values)), values, marker=".", linewidth=1, label=label)
        plotted = True
    if plotted:
        plt.xlabel("Sample")
        plt.ylabel("Throughput (Mbps)")
        plt.title("Application and OpenFlow throughput")
        plt.legend()
        plt.tight_layout()
        plt.savefig(out_dir / "01_throughput.png", dpi=160)
    plt.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results", help="Results root or one run directory")
    parser.add_argument("--out", default=None, help="Output directory (default: <results>/analysis)")
    args = parser.parse_args()

    root = Path(args.results).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: results directory does not exist: {root}", file=sys.stderr)
        return 2
    out_dir = Path(args.out).expanduser().resolve() if args.out else root / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    sender = read_csvs(root, ["**/sender*.csv"])
    receiver = read_csvs(root, ["**/receiver*.csv"])
    flows_all = read_csvs(root, ["**/flowstats*.csv"])
    events = read_csvs(root, ["**/controller_events*.csv"])

    if sender.empty and receiver.empty and flows_all.empty:
        print(f"No sender/receiver/flowstats CSV files found under: {root}")
        return 1

    inventory = pd.DataFrame([
        {"log_type": "sender", "rows": len(sender)},
        {"log_type": "receiver", "rows": len(receiver)},
        {"log_type": "flowstats", "rows": len(flows_all)},
        {"log_type": "controller_events", "rows": len(events)},
    ])
    inventory.to_csv(out_dir / "log_inventory.csv", index=False)

    # Use actual app measurements when available. Receiver totals are preferred
    # for received payload bytes and packet loss; sender is the fallback.
    app_source = receiver if not receiver.empty else sender
    app_bytes = max_numeric(app_source, ["rx_bytes", "tx_bytes", "payload_bytes", "byte_count", "bytes"])
    app_packets = max_numeric(app_source, ["rx_pkts", "tx_pkts", "packet_count", "packets"])
    app_rate_mean = mean_numeric(app_source, ["interval_mbps", "rate_mbps", "throughput_mbps", "rx_mbps", "tx_mbps"])
    app_loss = mean_numeric(receiver, ["loss_pct", "loss_percent", "packet_loss_pct"])

    selected_flows = select_s1_measurement_flow(flows_all)
    # Flowstats contain cumulative byte_count and interval delta_bytes. Do not
    # mix them: cumulative max is a total, delta_bytes is an interval amount.
    network_bytes = max_numeric(selected_flows, ["byte_count", "bytes"])
    if network_bytes is None:
        delta_bytes = numeric(selected_flows, ["delta_bytes"])
        if delta_bytes is not None and not delta_bytes.dropna().empty:
            network_bytes = float(delta_bytes.clip(lower=0).sum())
    network_rate_mean = mean_numeric(selected_flows, ["interval_mbps", "rate_mbps", "throughput_mbps"])
    network_packets = max_numeric(selected_flows, ["packet_count", "packets"])

    comparison_rows = [
        {"metric": "application_payload_bytes", "value": app_bytes, "unit": "bytes", "source": "receiver preferred; sender fallback"},
        {"metric": "network_bytes_s1_measurement_flow", "value": network_bytes, "unit": "bytes", "source": "s1 measurement flow only"},
        {"metric": "application_packets", "value": app_packets, "unit": "packets", "source": "receiver preferred; sender fallback"},
        {"metric": "network_packets_s1_measurement_flow", "value": network_packets, "unit": "packets", "source": "s1 measurement flow only"},
        {"metric": "application_mean_interval_mbps", "value": app_rate_mean, "unit": "Mbps", "source": "application CSV"},
        {"metric": "network_mean_interval_mbps", "value": network_rate_mean, "unit": "Mbps", "source": "s1 flowstats CSV"},
        {"metric": "receiver_mean_loss_pct", "value": app_loss, "unit": "%", "source": "receiver CSV; not inferred from OpenFlow"},
    ]
    if app_bytes is not None and network_bytes is not None and app_bytes > 0:
        comparison_rows.extend([
            {"metric": "network_to_application_byte_ratio", "value": network_bytes / app_bytes, "unit": "ratio", "source": "s1 network bytes / application payload bytes"},
            {"metric": "network_minus_application_bytes_pct", "value": (network_bytes - app_bytes) / app_bytes * 100, "unit": "%", "source": "relative to application payload bytes"},
        ])
    pd.DataFrame(comparison_rows).to_csv(out_dir / "comparison_metrics.csv", index=False)

    summaries = []
    for label, frame in [("sender", sender), ("receiver", receiver), ("flowstats_s1_measurement_flow", selected_flows), ("controller_events", events)]:
        if frame.empty:
            continue
        summaries.append({
            "log_type": label,
            "rows": len(frame),
            "mean_interval_mbps": mean_numeric(frame, ["interval_mbps", "rate_mbps", "throughput_mbps", "tx_mbps", "rx_mbps"]),
            "max_cumulative_or_logged_bytes": max_numeric(frame, ["byte_count", "rx_bytes", "tx_bytes", "bytes"]),
            "max_cumulative_or_logged_packets": max_numeric(frame, ["packet_count", "rx_pkts", "tx_pkts", "packets"]),
            "mean_loss_pct": mean_numeric(frame, ["loss_pct", "loss_percent", "packet_loss_pct"]),
        })
    pd.DataFrame(summaries).to_csv(out_dir / "summary_metrics.csv", index=False)

    plot_rates(out_dir, [("Sender", sender), ("Receiver", receiver), ("OpenFlow s1", selected_flows)])

    if not receiver.empty:
        loss_col = find_column(receiver, ["loss_pct", "loss_percent", "packet_loss_pct"])
        if loss_col:
            values = pd.to_numeric(receiver[loss_col], errors="coerce").dropna()
            if not values.empty:
                plt.figure(figsize=(9, 4))
                plt.plot(range(len(values)), values, marker=".")
                plt.xlabel("Sample")
                plt.ylabel("Packet loss (%)")
                plt.title("Receiver packet loss")
                plt.tight_layout()
                plt.savefig(out_dir / "02_packet_loss.png", dpi=160)
                plt.close()

    if app_bytes is not None and network_bytes is not None:
        chart = pd.DataFrame({"measurement": ["Application payload", "Network on s1"], "bytes": [app_bytes, network_bytes]})
        plt.figure(figsize=(7, 4))
        plt.bar(chart["measurement"], chart["bytes"])
        plt.ylabel("Bytes")
        plt.title("Application vs network byte counts")
        plt.tight_layout()
        plt.savefig(out_dir / "03_byte_comparison.png", dpi=160)
        plt.close()

    if not events.empty:
        events.to_csv(out_dir / "controller_events_combined.csv", index=False)

    print(f"Analysis complete. Outputs saved to: {out_dir}")
    print(f"Rows: sender={len(sender)}, receiver={len(receiver)}, flowstats={len(flows_all)}, events={len(events)}")
    print("Network counters are selected from s1 where switch identifiers are present; switch counters are not summed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

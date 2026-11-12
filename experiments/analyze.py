#!/usr/bin/env python3
"""Analyze experiment runs produced by experiments/run_experiment.py.

Reads every results/experiments/<run>/ folder (run_meta.json, sender_<id>.csv,
receiver_<id>.csv, flowstats.csv, controller_events.csv) and writes to
results/analysis/:

  summary_runs.csv          one row per run, all metrics
  summary_table.csv / .md   mean (and std over repeats) per config, static vs dynamic
  fig1_load_throughput.png  app vs network throughput vs offered load, static vs dynamic
  fig2_load_loss.png        packet loss vs offered load, static vs dynamic
  fig3_size_overhead.png    network-vs-app discrepancy vs payload size + theory line
  fig4_congestion_timeseries.png
  fig5_rate_limit_timeseries.png
  fig6_scenarios_bar.png    measurement-flow throughput and loss per scenario and mode

Network numbers come from the controller's per-flow counters:
  s1 (dpid 1) = what entered the network, s3 (dpid 3) = what left it towards h2.
Application numbers are UDP payload bytes; network numbers are Ethernet frame
bytes (payload + 42), so network / app should be about (size + 42) / size.

Usage:  python3 experiments/analyze.py [--runs results/experiments] [--out results/analysis]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OVERHEAD = 42
MEAS_PORT = 5001
BG_PORT = 5002
COLORS = {"static": "#4C72B0", "dynamic": "#DD8452"}
plt.rcParams.update({"figure.dpi": 150, "axes.grid": True, "grid.alpha": 0.3})


def read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path) if path.exists() and path.stat().st_size else pd.DataFrame()
    except (pd.errors.ParserError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def flow_id(src: str, port: int, dst: str = "10.0.0.2") -> str:
    return f"{src}>{dst}:{port}"


def flow_totals(flows: pd.DataFrame, events: pd.DataFrame, fid: str, dpid: int) -> tuple[float, float]:
    """Final packet and byte count of one flow on one switch.

    A rule can expire and be re-installed, which restarts its counters, so add up
    the final value of each rule lifetime (counter drops mark a new lifetime).
    """
    pkts = byts = 0.0
    if not flows.empty:
        f = flows[(flows["flow_id"] == fid) & (flows["dpid"] == dpid)].sort_values("ts")
        if not f.empty:
            new_life = f["bytes"].diff().fillna(0) < 0
            life = new_life.cumsum()
            last = f.groupby(life).last()
            pkts, byts = float(last["pkts"].sum()), float(last["bytes"].sum())
    # flow_removed carries the exact final counters of the last lifetime
    if not events.empty:
        rem = events[(events["event"] == "flow_removed") & (events["flow_id"] == fid)]
        for detail in rem["detail"].astype(str):
            kv = dict(p.split("=", 1) for p in detail.split() if "=" in p)
            if int(kv.get("dpid", -1)) == dpid:
                pkts = max(pkts, float(kv.get("pkts", 0)))
                byts = max(byts, float(kv.get("bytes", 0)))
    return pkts, byts


def active_seconds(df: pd.DataFrame, tcol: str, dcol: str, step: float = 1.0) -> float:
    """Seconds during which a flow actually moved data.

    Uses the log rows with a non-zero interval count: from the row before the
    first active row to the last active row. Do not divide by the configured
    duration: when the link is the bottleneck the sender is slowed down by
    back-pressure and the test takes longer than planned.
    """
    if df.empty or dcol not in df:
        return float("nan")
    d = df.sort_values(tcol).reset_index(drop=True)
    active = d.index[pd.to_numeric(d[dcol], errors="coerce").fillna(0) > 0]
    if len(active) == 0:
        return float("nan")
    first, last = active[0], active[-1]
    start = d.loc[first - 1, tcol] if first > 0 else d.loc[first, tcol] - step
    return max(float(d.loc[last, tcol] - start), 1e-6)


def flow_active_seconds(flows: pd.DataFrame, fid: str, dpid: int) -> float:
    if flows.empty:
        return float("nan")
    f = flows[(flows["flow_id"] == fid) & (flows["dpid"] == dpid)]
    return active_seconds(f, "ts", "delta_bytes")


def app_flow(folder: Path, tid: int) -> dict:
    """Sender + receiver numbers for one test id, using real elapsed times."""
    snd = read_csv(folder / f"sender_{tid}.csv")
    rcv = read_csv(folder / f"receiver_{tid}.csv")
    if snd.empty or rcv.empty:
        return {}
    s_last, r_last = snd.iloc[-1], rcv.iloc[-1]
    s_secs = float(s_last["time_s"])
    r_secs = active_seconds(rcv, "time_s", "interval_pkts")
    return {
        "tx_pkts": int(s_last["tx_pkts"]), "rx_pkts": int(r_last["rx_pkts"]),
        "lost_pkts": int(r_last["lost_pkts"]), "loss_pct": float(r_last["loss_pct"]),
        "send_secs": s_secs, "recv_secs": r_secs,
        "sent_mbps": s_last["tx_bytes"] * 8 / s_secs / 1e6 if s_secs else float("nan"),
        "app_mbps": r_last["rx_bytes"] * 8 / r_secs / 1e6,
        "rx_bytes": float(r_last["rx_bytes"]),
        "blocked": int(pd.to_numeric(snd.get("blocked", pd.Series([0])), errors="coerce").fillna(0).iloc[-1]),
        "delay_ms": pd.to_numeric(rcv["avg_delay_ms"], errors="coerce").dropna().mean(),
        "jitter_ms": pd.to_numeric(rcv["jitter_ms"], errors="coerce").dropna().mean(),
    }


def load_run(folder: Path) -> dict | None:
    meta_path = folder / "run_meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    tid = int(meta["test_id"])
    app = app_flow(folder, tid)
    if not app:
        print(f"skip {folder.name}: missing sender/receiver log", file=sys.stderr)
        return None
    flows = read_csv(folder / "flowstats.csv")
    events = read_csv(folder / "controller_events.csv")

    size = int(meta["packet_size"])
    fid = flow_id("10.0.0.1", int(meta.get("port", MEAS_PORT)))
    s1_p, s1_b = flow_totals(flows, events, fid, 1)
    s3_p, s3_b = flow_totals(flows, events, fid, 3)

    row = {
        "run": folder.name, "config": meta["name"], "scenario": meta.get("scenario", "none"),
        "mode": meta["controller_mode"], "repeat": meta.get("repeat", 1),
        "offered_mbps": float(meta["rate_mbps"]), "size_b": size,
        "planned_s": float(meta["duration"]),
        "declared_mbps": float(meta.get("target_rate_mbps", meta["rate_mbps"])),
        **{k: v for k, v in app.items() if k != "rx_bytes"},
        # Use the same time windows as the application: the 1 s flow-stats polls
        # are too coarse to time the start and end of a flow on their own.
        # s1 sees the packets as they are sent, s3 as they are received.
        "net_in_mbps": s1_b * 8 / app["send_secs"] / 1e6 if app["send_secs"] else float("nan"),
        "net_out_mbps": s3_b * 8 / app["recv_secs"] / 1e6 if app["recv_secs"] else float("nan"),
        "s1_pkts": s1_p, "s3_pkts": s3_p,
        "net_loss_pct": 100 * (s1_p - s3_p) / s1_p if s1_p else float("nan"),
        "rate_limit_events": int((events["event"] == "rate_limit_on").sum()) if not events.empty else 0,
        "expected_ratio": (size + OVERHEAD) / size,
    }
    # same packets counted twice: frame bytes at s3 vs payload bytes at the receiver
    row["net_app_ratio"] = (s3_b / app["rx_bytes"]) if app["rx_bytes"] else float("nan")
    row["discrepancy_pct"] = 100 * (row["net_app_ratio"] - 1)
    if meta.get("scenario") == "congestion":
        bg = app_flow(folder, tid + 5000)
        for k in ("sent_mbps", "app_mbps", "loss_pct", "blocked"):
            row["bg_" + k] = bg.get(k, float("nan"))
    row["_folder"] = folder
    row["_meta"] = meta
    return row


def timeseries(folder: Path, fid: str, dpid: int, t0: float) -> pd.DataFrame:
    flows = read_csv(folder / "flowstats.csv")
    if flows.empty:
        return pd.DataFrame(columns=["t", "rate"])
    f = flows[(flows["flow_id"] == fid) & (flows["dpid"] == dpid)]
    return pd.DataFrame({"t": f["ts"] - t0, "rate": f["rate_mbps"]}).sort_values("t")


def start_time(folder: Path, test_id: int) -> float | None:
    ev = read_csv(folder / "controller_events.csv")
    if ev.empty:
        return None
    for _, r in ev[ev["event"] == "test_start"].iterrows():
        try:
            if int(json.loads(r["detail"]).get("test_id", -1)) == test_id:
                return float(r["ts"])
        except (ValueError, TypeError):
            continue
    return None


def mark_events(ax, folder: Path, t0: float) -> None:
    ev = read_csv(folder / "controller_events.csv")
    if ev.empty:
        return
    labelled = set()
    for _, r in ev[ev["event"].isin(["rate_limit_on", "rate_limit_off"])].iterrows():
        name = r["event"]
        ax.axvline(r["ts"] - t0, color="red" if name.endswith("on") else "green", ls="--", lw=1,
                   label=name if name not in labelled else None)
        labelled.add(name)


def mean_std(df: pd.DataFrame, by: list[str], col: str) -> pd.DataFrame:
    g = df.groupby(by)[col].agg(["mean", "std"]).reset_index()
    g["std"] = g["std"].fillna(0)
    return g


def fig_load(df: pd.DataFrame, out: Path) -> None:
    d = df[df["config"] == "load_sweep"]
    if d.empty:
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for mode in ("static", "dynamic"):
        m = d[d["mode"] == mode]
        if m.empty:
            continue
        for col, ls, lab in (("sent_mbps", ":", "achieved by sender"), ("app_mbps", "-", "app (receiver)"),
                             ("net_out_mbps", "--", "network (s3 flow stats)")):
            g = mean_std(m, ["offered_mbps"], col)
            ax.errorbar(g["offered_mbps"], g["mean"], yerr=g["std"], ls=ls, marker="o", capsize=3,
                        color=COLORS[mode], label=f"{mode}: {lab}")
    top = d["offered_mbps"].max()
    ax.plot([0, top], [0, top], color="grey", lw=0.8, ls=":", label="offered = delivered")
    ax.axhline(20, color="black", lw=0.8, ls="-.", label="core link 20 Mbps")
    ax.set(xlabel="Offered load (Mbit/s)", ylabel="Throughput (Mbit/s)",
           title="Application vs network throughput across offered load")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "fig1_load_throughput.png")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for mode in ("static", "dynamic"):
        m = d[d["mode"] == mode]
        if m.empty:
            continue
        g = mean_std(m, ["offered_mbps"], "loss_pct")
        ax.errorbar(g["offered_mbps"], g["mean"], yerr=g["std"], marker="o", capsize=3,
                    color=COLORS[mode], label=mode)
    ax.axvline(20, color="black", lw=0.8, ls="-.", label="core link 20 Mbps")
    ax.set(xlabel="Offered load (Mbit/s)", ylabel="Packet loss (%)", title="Packet loss vs offered load")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "fig2_load_loss.png")
    plt.close(fig)


def fig_size(df: pd.DataFrame, out: Path) -> None:
    d = df[df["config"] == "size_sweep"]
    if d.empty:
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for mode in ("static", "dynamic"):
        m = d[d["mode"] == mode]
        if m.empty:
            continue
        g = mean_std(m, ["size_b"], "discrepancy_pct")
        ax.errorbar(g["size_b"], g["mean"], yerr=g["std"], marker="o", capsize=3,
                    color=COLORS[mode], label=f"measured ({mode})")
    xs = list(range(150, 1501, 25))
    ax.plot(xs, [100 * OVERHEAD / x for x in xs], color="black", ls=":", label="theory: 42 / payload")
    ax.set(xlabel="UDP payload size (bytes)", ylabel="Network − app bytes (% of app)",
           title="Header overhead: network vs application byte counts")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "fig3_size_overhead.png")
    plt.close(fig)


def fig_timeseries(df: pd.DataFrame, out: Path, config: str, fname: str, title: str) -> None:
    d = df[df["config"] == config]
    if d.empty:
        return
    modes = [m for m in ("static", "dynamic") if (d["mode"] == m).any()]
    fig, axes = plt.subplots(1, len(modes), figsize=(6 * len(modes), 4.2), sharey=True, squeeze=False)
    for ax, mode in zip(axes[0], modes):
        run = d[d["mode"] == mode].sort_values("repeat").iloc[0]
        folder, meta = run["_folder"], run["_meta"]
        tid = int(meta["test_id"])
        t0 = start_time(folder, tid)
        if t0 is None:
            continue
        fid = flow_id("10.0.0.1", int(meta.get("port", MEAS_PORT)))
        for dpid, ls, lab in ((1, ":", "measurement flow at s1 (sent)"), (3, "-", "measurement flow at s3 (delivered)")):
            ts = timeseries(folder, fid, dpid, t0)
            ax.plot(ts["t"], ts["rate"], ls=ls, color=COLORS[mode], label=lab)
        if meta.get("scenario") == "congestion":
            ts = timeseries(folder, flow_id("10.0.0.3", BG_PORT), 3, t0)
            ax.plot(ts["t"], ts["rate"], color="grey", label="background flow h3 at s3")
        mark_events(ax, folder, t0)
        ax.set(title=f"{mode} mode", xlabel="Time since test_start (s)")
        ax.legend(fontsize=7)
    axes[0][0].set_ylabel("Rate from flow stats (Mbit/s, frame bytes)")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out / fname)
    plt.close(fig)


def fig_scenarios(df: pd.DataFrame, out: Path) -> None:
    d = df[df["config"].isin(["rate_limit", "congestion", "packet_loss"])]
    if d.empty:
        return
    configs = [c for c in ("rate_limit", "congestion", "packet_loss") if (d["config"] == c).any()]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2))
    width = 0.38
    for i, mode in enumerate(("static", "dynamic")):
        m = d[d["mode"] == mode]
        tp = [m[m["config"] == c]["app_mbps"].mean() for c in configs]
        ls = [m[m["config"] == c]["loss_pct"].mean() for c in configs]
        xs = [x + (i - 0.5) * width for x in range(len(configs))]
        axes[0].bar(xs, tp, width, color=COLORS[mode], label=mode)
        axes[1].bar(xs, ls, width, color=COLORS[mode], label=mode)
        dl = [m[m["config"] == c]["delay_ms"].mean() for c in configs]
        axes[2].bar(xs, dl, width, color=COLORS[mode], label=mode)
    for ax, ylab in zip(axes, ("Measurement-flow throughput (Mbit/s)", "Measurement-flow loss (%)",
                               "Measurement-flow one-way delay (ms)")):
        ax.set_xticks(range(len(configs)))
        ax.set_xticklabels(configs)
        ax.set_ylabel(ylab)
        ax.legend()
    fig.suptitle("Scenarios: static baseline vs dynamic controller")
    fig.tight_layout()
    fig.savefig(out / "fig6_scenarios_bar.png")
    plt.close(fig)


def summary_table(df: pd.DataFrame, out: Path) -> None:
    cols = ["sent_mbps", "app_mbps", "net_in_mbps", "net_out_mbps", "loss_pct", "net_loss_pct",
            "discrepancy_pct", "expected_ratio", "net_app_ratio", "delay_ms", "jitter_ms",
            "blocked", "rate_limit_events", "bg_sent_mbps", "bg_app_mbps"]
    cols = [c for c in cols if c in df]
    keys = ["config", "scenario", "offered_mbps", "declared_mbps", "size_b", "mode"]
    g = df.groupby(keys, dropna=False)[cols].mean().round(3).reset_index()
    g.insert(len(keys), "runs", df.groupby(keys, dropna=False).size().values)
    g.to_csv(out / "summary_table.csv", index=False)
    with (out / "summary_table.md").open("w") as fh:
        fh.write("| " + " | ".join(g.columns) + " |\n|" + "---|" * len(g.columns) + "\n")
        for _, r in g.iterrows():
            fh.write("| " + " | ".join(str(v) for v in r.values) + " |\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, default=ROOT / "results" / "experiments")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "analysis")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows = [r for f in sorted(args.runs.iterdir()) if f.is_dir() for r in [load_run(f)] if r]
    if not rows:
        print(f"no complete runs found in {args.runs}", file=sys.stderr)
        return 1
    df = pd.DataFrame(rows)
    df.drop(columns=["_folder", "_meta"]).round(4).to_csv(args.out / "summary_runs.csv", index=False)
    summary_table(df, args.out)
    fig_load(df, args.out)
    fig_size(df, args.out)
    fig_timeseries(df, args.out, "congestion", "fig4_congestion_timeseries.png",
                   "Congestion: h3 floods 20 Mbps (declares 8) while h1 sends 10 Mbps")
    fig_timeseries(df, args.out, "rate_limit", "fig5_rate_limit_timeseries.png",
                   "Rate limit: h1 sends 10 Mbps, declares 5 Mbps")
    fig_scenarios(df, args.out)
    print(f"{len(df)} runs analysed -> {args.out}")
    for p in sorted(args.out.glob("*")):
        print("  ", p.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

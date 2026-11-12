#!/usr/bin/env python3
"""Run Project 26 experiments automatically (controller + Mininet + UDP app).

For every run it:
  1. cleans Mininet (sudo mn -c)  -- BEFORE starting Ryu, because mn -c kills ryu-manager
  2. starts Ryu with sdn/controller.py in the requested mode (static|dynamic)
  3. starts sdn/topology.py and drives its Mininet CLI through stdin
  4. applies the scenario, starts receiver(s) on h2 and sender(s) on h1/h3
  5. saves everything in results/<run_name>/ and writes run_meta.json

A config may give a single value or a list for controller_mode, rate_mbps and
packet_size; the runner expands the full combination and repeats it.

Examples:
  python3 experiments/run_experiment.py --config experiments/configs/normal.json
  python3 experiments/run_experiment.py --all
  python3 experiments/run_experiment.py --all --dry-run       # print the plan only
  python3 experiments/run_experiment.py --config experiments/configs/load_sweep.json --repeats 1

Scenarios:
  none        measurement flow h1 -> h2 only
  congestion  + background flow h3 -> h2 (bg_rate_mbps, declares bg_target_mbps to
              the controller; in dynamic mode the controller meters it down)
  packet_loss + netem loss (loss_pct %) on the s2 -> s3 core link
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ("none", "congestion", "packet_loss")
BG_PORT = 5002
BG_TEST_ID_OFFSET = 5000          # background flow test id = measurement id + 5000
LOSS_INTF = ("s2", "s2-eth2")     # s2 side of the s2-s3 core link (see sdn/topology.py)
CORE_BW = 20                      # Mbps, must match sdn/topology.py


def as_list(value):
    return value if isinstance(value, list) else [value]


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    required = ["name", "controller_mode", "rate_mbps", "packet_size", "duration", "test_id"]
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ValueError(f"{path}: missing config fields: {', '.join(missing)}")
    cfg.setdefault("scenario", "none")
    cfg.setdefault("repeats", 1)
    if cfg["scenario"] not in SCENARIOS:
        raise ValueError(f"{path}: scenario must be one of {SCENARIOS}")
    for mode in as_list(cfg["controller_mode"]):
        if mode not in ("static", "dynamic"):
            raise ValueError(f"{path}: controller_mode must be static or dynamic")
    for rate in as_list(cfg["rate_mbps"]):
        if float(rate) <= 0:
            raise ValueError(f"{path}: rate_mbps must be > 0")
    for size in as_list(cfg["packet_size"]):
        if not 20 <= int(size) <= 1472:
            raise ValueError(f"{path}: packet_size must be 20-1472 bytes")
    if float(cfg["duration"]) <= 0:
        raise ValueError(f"{path}: duration must be > 0")
    return cfg


def expand(cfg: dict, repeats: int) -> list[dict]:
    """One dict per individual run (mode x rate x size x repeat)."""
    runs = []
    combos = itertools.product(as_list(cfg["controller_mode"]), as_list(cfg["rate_mbps"]),
                               as_list(cfg["packet_size"]), range(1, repeats + 1))
    for i, (mode, rate, size, rep) in enumerate(combos):
        run = dict(cfg)
        run.update(controller_mode=mode, rate_mbps=float(rate), packet_size=int(size),
                   repeat=rep, test_id=int(cfg["test_id"]) + i)
        runs.append(run)
    return runs


def safe(value) -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value).strip())
    return text.strip("_") or "x"


def run_name(run: dict) -> str:
    rate = f"{run['rate_mbps']:g}".replace(".", "p")
    return (f"{safe(run['name'])}_{run['controller_mode']}_r{rate}_s{run['packet_size']}"
            f"_rep{run['repeat']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")


def build_cli_script(run: dict, out: Path, ctrl_url: str) -> str:
    """Commands typed into the Mininet CLI. Paths are relative to the project root."""
    tid, port = int(run["test_id"]), int(run.get("port", 5001))
    dur = float(run["duration"])
    dst = run.get("destination", "10.0.0.2")
    common = f"--log-dir {out} --ctrl-url {ctrl_url}"
    cmds = ["sh sleep 3", "pingall"]   # give switches time to connect; pingall teaches MACs

    if run["scenario"] == "packet_loss":
        node, intf = LOSS_INTF
        cmds.append(f"py {node}.intf('{intf}').config(bw={CORE_BW}, loss={float(run.get('loss_pct', 5))})")

    cmds.append(f"h2 python3 app/receiver.py --port {port} --test-id {tid} {common} &")

    if run["scenario"] == "congestion":
        bg_id = tid + BG_TEST_ID_OFFSET
        bg_rate = float(run.get("bg_rate_mbps", CORE_BW))
        bg_target = float(run.get("bg_target_mbps", bg_rate / 2))
        cmds.append(f"h2 python3 app/receiver.py --port {BG_PORT} --test-id {bg_id} {common} &")
        cmds.append("sh sleep 1")
        cmds.append(f"h3 python3 app/sender.py --dst {dst} --port {BG_PORT} --rate {bg_rate} "
                    f"--target-rate {bg_target} --size 1000 --duration {dur + 4} --test-id {bg_id} {common} &")
        cmds.append("sh sleep 2")     # let the background flow fill the link first
    else:
        cmds.append("sh sleep 1")

    extra = f" --target-rate {float(run['target_rate_mbps'])}" if "target_rate_mbps" in run else ""
    cmds.append(f"h1 python3 app/sender.py --dst {dst} --port {port} --rate {run['rate_mbps']}"
                f" --size {run['packet_size']} --duration {dur} --test-id {tid}{extra} {common}")
    cmds.append("sh sleep 5")         # receivers finish (END packet / idle timeout) and post test_end
    for sw in ("s1", "s2", "s3"):
        cmds.append(f"sh ovs-ofctl -O OpenFlow13 dump-flows {sw} > {out}/dump_flows_{sw}.txt")
        cmds.append(f"sh ovs-ofctl -O OpenFlow13 dump-meters {sw} > {out}/dump_meters_{sw}.txt 2>&1")
    cmds.append("exit")
    return "\n".join(cmds) + "\n"


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, PermissionError):
        pass
    except subprocess.TimeoutExpired:
        subprocess.run(["sudo", "-n", "kill", "-9", f"-{proc.pid}"], check=False)
        proc.wait()


def cleanup_mininet(root: Path) -> None:
    subprocess.run(["sudo", "-n", "mn", "-c"], cwd=root, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_one(run: dict, args: argparse.Namespace) -> bool:
    name = run_name(run)
    out_abs = args.results_root / name
    out_rel = os.path.relpath(out_abs, args.project_root)
    ctrl_url = f"http://{args.rest_host}:{args.rest_port}"      # ctrl_client appends /test
    script = build_cli_script(run, Path(out_rel), ctrl_url)
    ryu_cmd = [args.ryu_manager, "--ofp-tcp-listen-port", "6653",
               "--wsapi-host", "0.0.0.0", "--wsapi-port", str(args.rest_port), "sdn/controller.py"]

    print(f"\n=== {name} ===")
    if args.dry_run:
        print("Ryu:     ", f"CONTROLLER_MODE={run['controller_mode']} RESULTS_DIR={out_rel}", " ".join(ryu_cmd))
        print("Topology: sudo python3 sdn/topology.py")
        print("Mininet CLI input:\n  " + script.replace("\n", "\n  "))
        return True

    out_abs.mkdir(parents=True, exist_ok=False)
    (out_abs / "run_meta.json").write_text(json.dumps(run, indent=2))
    env = dict(os.environ, CONTROLLER_MODE=run["controller_mode"], RESULTS_DIR=str(out_rel))
    ryu = topo = None
    ryu_log = (out_abs / "controller_stdout.log").open("w")
    topo_log = (out_abs / "topology_stdout.log").open("w")
    ok = False
    try:
        cleanup_mininet(args.project_root)              # must happen BEFORE Ryu starts
        ryu = subprocess.Popen(ryu_cmd, cwd=args.project_root, env=env, stdout=ryu_log,
                               stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(args.controller_wait)
        if ryu.poll() is not None:
            raise RuntimeError(f"Ryu exited early (code {ryu.returncode}); see {out_rel}/controller_stdout.log")

        # No start_new_session here: sudo caches the password per terminal, and a new
        # session has no terminal, so 'sudo -n' would refuse to run.
        topo = subprocess.Popen(["sudo", "-n", "python3", "sdn/topology.py"], cwd=args.project_root,
                                stdin=subprocess.PIPE, stdout=topo_log, stderr=subprocess.STDOUT,
                                text=True)
        timeout = float(run["duration"]) + 90
        try:
            topo.communicate(script, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Mininet did not finish within {timeout:.0f} s; see {out_rel}/topology_stdout.log")

        time.sleep(2)                                   # last flow-stats poll / flow_removed events
        tid = int(run["test_id"])
        need = [f"sender_{tid}.csv", f"receiver_{tid}.csv", "flowstats.csv", "controller_events.csv"]
        missing = [n for n in need if not (out_abs / n).exists()]
        if missing:
            raise RuntimeError(f"missing logs: {', '.join(missing)}; see {out_rel}/topology_stdout.log")
        print(f"OK -> {out_rel}")
        ok = True
    except RuntimeError as exc:
        print(f"FAILED {name}: {exc}", file=sys.stderr)
    finally:
        if topo is not None and topo.poll() is None:
            subprocess.run(["sudo", "-n", "pkill", "-f", "sdn/topology.py"], check=False)
            try:
                topo.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        stop(ryu)
        ryu_log.close()
        topo_log.close()
        cleanup_mininet(args.project_root)
        # receiver/sender ran as root inside Mininet; give the files back to the user
        subprocess.run(["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(out_abs)], check=False)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pick = ap.add_mutually_exclusive_group(required=True)
    pick.add_argument("--config", type=Path, help="one JSON config")
    pick.add_argument("--all", action="store_true", help="every JSON file in experiments/configs/")
    ap.add_argument("--repeats", type=int, default=None, help="override the config's repeats")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without running anything")
    ap.add_argument("--results-root", type=Path, default=ROOT / "results" / "experiments")
    ap.add_argument("--ryu-manager", default="ryu-manager")
    ap.add_argument("--rest-host", default="10.0.0.254", help="controller address seen from hosts (NAT node)")
    ap.add_argument("--rest-port", type=int, default=8080)
    ap.add_argument("--controller-wait", type=float, default=4.0)
    args = ap.parse_args()
    args.project_root = ROOT
    args.results_root = args.results_root.expanduser().resolve()

    paths = sorted((ROOT / "experiments" / "configs").glob("*.json")) if args.all else [args.config.resolve()]
    if not paths:
        ap.error("no configs found in experiments/configs/")

    if not args.dry_run:
        if shutil.which(args.ryu_manager) is None:
            ap.error(f"cannot find {args.ryu_manager!r}; activate the Ryu environment first")
        subprocess.run(["sudo", "-v"], check=True)     # ask for the password once, up front

    runs = []
    for path in paths:
        try:
            cfg = load_config(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"SKIP {path.name}: {exc}", file=sys.stderr)
            continue
        runs += expand(cfg, args.repeats or int(cfg["repeats"]))
    print(f"{len(runs)} run(s) planned")

    failed = 0
    for i, run in enumerate(runs, 1):
        print(f"[{i}/{len(runs)}]", end="")
        if not args.dry_run:
            subprocess.run(["sudo", "-v"], check=False)  # keep sudo alive during long sweeps
        if not run_one(run, args):
            failed += 1
    print(f"\nFinished: {len(runs) - failed} succeeded, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

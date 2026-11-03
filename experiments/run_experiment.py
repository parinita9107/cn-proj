#!/usr/bin/env python3
"""Run Project 26 experiments through the existing Mininet CLI topology.

This runner matches the project's D1 interfaces:
  * Ryu: ryu-manager --wsapi-host 0.0.0.0 --wsapi-port 8080 --observe-links
  * Mininet topology: sdn/topology.py (opens the Mininet CLI)
  * Receiver: app/receiver.py --port --test-id --log-dir --ctrl-url
  * Sender: app/sender.py --dst --port --rate --size --duration --test-id --log-dir --ctrl-url

Run from an activated environment containing Ryu. Mininet is launched with sudo.
"""

from __future__ import annotations

import argparse
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


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    required = ["name", "controller_mode", "rate_mbps", "packet_size", "duration", "scenario", "test_id"]
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ValueError(f"{path}: missing config fields: {', '.join(missing)}")
    if not str(cfg["name"]).strip():
        raise ValueError(f"{path}: name must not be empty")
    if cfg["controller_mode"] not in ("static", "dynamic"):
        raise ValueError(f"{path}: controller_mode must be static or dynamic")
    for key in ("rate_mbps", "packet_size", "duration", "test_id"):
        if float(cfg[key]) <= 0:
            raise ValueError(f"{path}: {key} must be greater than zero")
    if int(cfg["packet_size"]) < 20:
        raise ValueError(f"{path}: packet_size must be at least 20 bytes for the project header")
    return cfg


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value.strip())
    return cleaned.strip("_") or "experiment"


def wait_for_controller(proc: subprocess.Popen, log_path: Path, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Ryu exited with code {proc.returncode}; inspect {log_path}")
        time.sleep(0.25)


def build_cli_script(cfg: dict, result_dir: Path, project_root: Path, rest_url: str) -> str:
    app_dir = project_root / "app"
    # Commands are sent to Mininet's CLI stdin. '&' backgrounds the receiver,
    # allowing the sender command to run while the receiver listens on h2.
    commands = [
        "pingall",
        f"h2 python3 {app_dir / 'receiver.py'} --port {int(cfg.get('port', 5001))} --test-id {int(cfg['test_id'])} --log-dir {result_dir} --ctrl-url {rest_url} &",
        "sh sleep 2",
        f"h1 python3 {app_dir / 'sender.py'} --dst {cfg.get('destination', '10.0.0.2')} --port {int(cfg.get('port', 5001))} --rate {float(cfg['rate_mbps'])} --size {int(cfg['packet_size'])} --duration {float(cfg['duration'])} --test-id {int(cfg['test_id'])} --log-dir {result_dir} --ctrl-url {rest_url}",
        "sh ovs-ofctl -O OpenFlow13 dump-flows s1",
        "exit",
    ]
    return "\n".join(commands) + "\n"


def run_one(config_path: Path, cfg: dict, args: argparse.Namespace) -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_name = f"{safe_name(str(cfg['name']))}_{cfg['controller_mode']}_{stamp}"
    result_dir = (args.results_root / run_name).resolve()
    result_dir.mkdir(parents=True, exist_ok=False)

    rest_host = args.rest_host
    rest_url = f"http://{rest_host}:{args.rest_port}/test"
    env = os.environ.copy()
    env["CONTROLLER_MODE"] = str(cfg["controller_mode"])
    env["PROJECT26_TEST_ID"] = str(cfg["test_id"])
    env["RESULTS_DIR"] = str(result_dir)

    controller_log_path = result_dir / "controller_stdout.log"
    topology_log_path = result_dir / "topology_stdout.log"
    controller = None
    topology = None
    controller_log = None
    topology_log = None

    print(f"\n=== {run_name} ===")
    print("Config:", json.dumps(cfg, indent=2))
    print("Results:", result_dir)

    if args.dry_run:
        print("DRY RUN: no processes started and no result directory should be created beyond this preview.")
        print("Controller:", args.ryu_manager, "--wsapi-host", "0.0.0.0", "--wsapi-port", str(args.rest_port), "--observe-links", "sdn/controller.py")
        print("Topology:", "sudo -n python3 sdn/topology.py")
        print("Mininet CLI commands:\n" + build_cli_script(cfg, result_dir, args.project_root, rest_url))
        return 0

    try:
        # Ensure sudo credentials are available before piping commands to Mininet.
        subprocess.run(["sudo", "-v"], check=True)
        if shutil.which(args.ryu_manager) is None and not Path(args.ryu_manager).exists():
            raise RuntimeError(f"Cannot find {args.ryu_manager!r}. Activate the Ryu virtual environment first.")

        controller_log = controller_log_path.open("w", encoding="utf-8")
        controller_cmd = [args.ryu_manager, "--wsapi-host", "0.0.0.0", "--wsapi-port", str(args.rest_port), "--observe-links", "sdn/controller.py"]
        controller = subprocess.Popen(controller_cmd, cwd=args.project_root, env=env, stdout=controller_log, stderr=subprocess.STDOUT, start_new_session=True)
        wait_for_controller(controller, controller_log_path, args.controller_wait)

        topology_log = topology_log_path.open("w", encoding="utf-8")
        topology_cmd = ["sudo", "-n", "python3", "sdn/topology.py"]
        topology = subprocess.Popen(topology_cmd, cwd=args.project_root, env=env, stdin=subprocess.PIPE, stdout=topology_log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        script = build_cli_script(cfg, result_dir, args.project_root, rest_url)
        try:
            topology.communicate(script, timeout=float(cfg["duration"]) + args.topology_timeout_extra)
        except subprocess.TimeoutExpired:
            os.killpg(topology.pid, signal.SIGTERM)
            try:
                topology.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(topology.pid, signal.SIGKILL)
                topology.wait()
            raise RuntimeError(f"Mininet did not finish in time; inspect {topology_log_path}")

        if topology.returncode != 0:
            raise RuntimeError(f"Mininet topology exited with code {topology.returncode}; inspect {topology_log_path}")

        # Controller writes flowstats/events. Allow its final buffers to flush.
        time.sleep(2)
        required_logs = [result_dir / f"sender_{int(cfg['test_id'])}.csv", result_dir / f"receiver_{int(cfg['test_id'])}.csv", result_dir / "flowstats.csv", result_dir / "controller_events.csv"]
        missing_logs = [str(path.name) for path in required_logs if not path.exists()]
        if missing_logs:
            raise RuntimeError("Experiment ended but expected CSV files are missing: " + ", ".join(missing_logs) + f". Inspect {topology_log_path} and {controller_log_path}")
        print("Experiment completed. CSV files:")
        for path in required_logs:
            print(f"  {path.name}: {path.stat().st_size} bytes")
        return 0

    finally:
        if topology is not None and topology.poll() is None:
            os.killpg(topology.pid, signal.SIGTERM)
            try:
                topology.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(topology.pid, signal.SIGKILL)
                topology.wait()
        if controller is not None and controller.poll() is None:
            os.killpg(controller.pid, signal.SIGTERM)
            try:
                controller.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(controller.pid, signal.SIGKILL)
                controller.wait()
        if controller_log:
            controller_log.close()
        if topology_log:
            topology_log.close()
        # Mininet cleanup is safe even if topology startup failed part-way through.
        subprocess.run(["sudo", "-n", "mn", "-c"], cwd=args.project_root, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--config", help="One JSON configuration")
    selection.add_argument("--all", action="store_true", help="Run every JSON file in experiments/configs")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without starting controller or Mininet")
    parser.add_argument("--project-root", type=Path, default=ROOT, help="Project root (default: detected from this script)")
    parser.add_argument("--results-root", type=Path, default=None, help="Results parent (default: <project-root>/results)")
    parser.add_argument("--ryu-manager", default="ryu-manager", help="Ryu manager executable")
    parser.add_argument("--rest-host", default="10.0.0.4", help="REST host reachable from Mininet hosts")
    parser.add_argument("--rest-port", type=int, default=8080)
    parser.add_argument("--controller-wait", type=float, default=4.0)
    parser.add_argument("--topology-timeout-extra", type=float, default=30.0)
    args = parser.parse_args()
    args.project_root = args.project_root.expanduser().resolve()
    args.results_root = (args.results_root or (args.project_root / "results")).expanduser().resolve()

    if not args.project_root.is_dir():
        parser.error(f"project root does not exist: {args.project_root}")
    config_dir = args.project_root / "experiments" / "configs"
    paths = sorted(config_dir.glob("*.json")) if args.all else [Path(args.config).expanduser().resolve()]
    if not paths:
        parser.error(f"no JSON configuration files found in {config_dir}")

    failures = 0
    for path in paths:
        try:
            cfg = load_config(path)
            # Current topology is D1-specific. Do not silently claim to implement
            # congestion/loss/link-failure scenarios that topology.py does not apply.
            if cfg.get("scenario", "none") not in ("none", "baseline", "static"):
                raise ValueError(f"Scenario {cfg['scenario']!r} is not automated by the current topology runner. Use scenario 'none' for D1 or implement that scenario in topology.py first.")
            if args.dry_run:
                stamp = "DRYRUN"
                preview_dir = args.results_root / f"{safe_name(str(cfg['name']))}_{cfg['controller_mode']}_{stamp}"
                print(f"\n=== {path.name} ===")
                print("Validated config:", json.dumps(cfg, indent=2))
                print("Would write results to:", preview_dir)
                print("No processes started; no directories created.")
                continue
            if run_one(path, cfg, args) != 0:
                failures += 1
        except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, RuntimeError) as exc:
            failures += 1
            print(f"FAILED {path}: {exc}", file=sys.stderr)
    print(f"\nFinished: {len(paths) - failures} succeeded, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

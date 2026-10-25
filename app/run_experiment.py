
import os
import sys
import json
import time
import signal
import argparse
import subprocess
from datetime import datetime


ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)


def load_config(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def run_command(command, **kwargs):
    print("Running:", " ".join(command))
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        **kwargs
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_dir = os.path.join(
        ROOT, "experiments", "configs"
    )

    if args.all:
        configs = [
            os.path.join(config_dir, name)
            for name in sorted(os.listdir(config_dir))
            if name.endswith(".json")
        ]
    elif args.config:
        configs = [os.path.abspath(args.config)]
    else:
        parser.error("Use --config FILE or --all")

    if not configs:
        parser.error("No configuration files found")

    for config_path in configs:
        config = load_config(config_path)

        name = config["name"]
        mode = config["controller_mode"]

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_dir = os.path.join(
            ROOT, "results", f"{name}_{mode}_{run_id}"
        )
        os.makedirs(result_dir, exist_ok=False)

        print("\nExperiment:", name)
        print("Mode:", mode)
        print("Results:", result_dir)

        controller_cmd = [
            "ryu-manager",
            "--observe-links",
            "sdn/controller.py"
        ]

        topology_cmd = [
            "sudo",
            "python3",
            "sdn/topology.py"
        ]

        if args.dry_run:
            print("Controller command:", " ".join(controller_cmd))
            print("Topology command:", " ".join(topology_cmd))
            print("Configuration:", json.dumps(config, indent=2))
            print("Dry run only. No processes started.")
            continue

        env = os.environ.copy()
        env["CONTROLLER_MODE"] = mode
        env["PROJECT26_TEST_ID"] = str(config["test_id"])

        controller_log_path = os.path.join(
            result_dir, "controller.log"
        )

        controller = None

        try:
            with open(controller_log_path, "w", encoding="utf-8") as log:
                controller = subprocess.Popen(
                    controller_cmd,
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True
                )

                time.sleep(3)

                if controller.poll() is not None:
                    raise RuntimeError(
                        "Controller stopped. Check controller.log"
                    )

                print(
                    "\nThe controller is running."
                    "\nStart the Mininet topology in another terminal:"
                )
                print(" ".join(topology_cmd))

                print(
                    "\nIMPORTANT: This starter does not automatically "
                    "launch the Mininet hosts."
                )
                print(
                    "Start the receiver on h2 and then the sender on h1 "
                    "using your team's agreed commands."
                )
                input(
                    "\nAfter the test finishes and CSV logs are collected, "
                    "press Enter to stop the controller..."
                )

        except KeyboardInterrupt:
            print("\nExperiment interrupted.")

        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            print("Experiment error:", error, file=sys.stderr)
            return 1

        finally:
            if controller is not None and controller.poll() is None:
                os.killpg(controller.pid, signal.SIGTERM)

                try:
                    controller.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(controller.pid, signal.SIGKILL)

            subprocess.run(
                ["sudo", "mn", "-c"],
                cwd=ROOT,
                check=False
            )

        print("Experiment session finished:", result_dir)

    print("\nAnalyze the collected logs with:")
    print("python3 experiments/analyze.py --results results")


if __name__ == "__main__":
    main()

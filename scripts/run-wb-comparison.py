#!/usr/bin/env python3
"""Run repeatable white-box EvoMaster experiments against a dist SUT.

Example (requires the SUT, controller, agent, and EvoMaster CLI jars):
    python scripts/run-wb-comparison.py --app gdpr-kv --runs 3 --seed 42 \
        --evomaster-jar /path/to/evomaster.jar

Each cycle runs all three DynamoDB modes with the same seed. Without --seed,
each cycle gets a different randomly generated seed. Each mode retains
EvoMaster's generated tests and statistics, plus raw process logs.
"""

import argparse
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mode:
    """Describe one DynamoDB configuration for a comparable run."""

    name: str
    instrument_dynamodb: bool
    dynamodb_heuristics: bool
    extract_dynamodb_execution_info: bool
    generate_dynamodb_data: bool


MODES = (
    Mode("no-ddb-heuristics", False, False, False, False),
    Mode("ddb-heuristics-no-insertions", True, True, True, False),
    Mode("ddb-heuristics-with-insertions", True, True, True, True),
)


def positive_int(value):
    """Parse a strictly positive command-line integer."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args():
    """Read experiment settings from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, help="application name, e.g. gdpr-kv")
    parser.add_argument("--runs", type=positive_int, required=True,
                        help="number of three-mode comparison cycles")
    parser.add_argument("--seed", type=int,
                        help="fixed seed for every cycle (default: a random seed per cycle)")
    parser.add_argument("--dist", type=Path, default=PROJECT_DIR / "dist")
    parser.add_argument("--evomaster-jar", type=Path,
                        help="EvoMaster CLI jar (default: <dist>/evomaster.jar)")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_DIR / "results")
    parser.add_argument("--java", default="java", help="Java command for the controller and SUT")
    parser.add_argument("--em-java", help="Java command for EvoMaster (default: --java)")
    parser.add_argument("--controller-port", type=positive_int, default=40100)
    parser.add_argument("--startup-timeout", type=positive_int, default=300,
                        help="seconds to wait for the controller (default: 300)")
    parser.add_argument("--max-time", default="600s", help="EvoMaster search budget (default: 600s)")
    args = parser.parse_args()
    if not args.app or Path(args.app).name != args.app or args.app in (".", ".."):
        parser.error("--app must be a single application name")
    if args.controller_port >= 65535:
        parser.error("--controller-port must leave room for the SUT port")
    if args.seed is not None and not 0 <= args.seed < 2**63:
        parser.error("--seed must be a non-negative signed 64-bit integer")
    return args


def stop_process(process):
    """Stop a process and its children, including a SUT launched by the controller."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def controller_available(port):
    """Check whether the local controller accepts TCP connections."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def validate_java(command, option):
    """Report a missing Java executable before creating a run directory."""
    if shutil.which(command) is None:
        raise SystemExit(f"Java executable for {option} was not found: {command}")


def validate_evomaster_jar(path):
    """Require a self-executable EvoMaster jar rather than the Maven core library."""
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8")
    except (OSError, KeyError, UnicodeError, zipfile.BadZipFile) as error:
        raise SystemExit(f"Invalid EvoMaster jar {path}: {error}") from error
    if not any(line.lower().startswith("main-class:") for line in manifest.splitlines()):
        raise SystemExit(f"EvoMaster jar is not executable (no Main-Class): {path}. "
                         "Use core/target/evomaster.jar instead.")


def controller_warnings(log_path):
    """Surface instrumentation failures that do not change EvoMaster's exit code."""
    if not log_path.exists():
        return []
    log = log_path.read_text(encoding="utf-8", errors="replace")
    if "NotSerializableException: software.amazon.awssdk.services.dynamodb.model." in log:
        return ["DynamoDB request serialization failed; coverage may be incomplete. "
                "See controller.log and statistics.csv."]
    return []


def run_once(args, cycle_number, seed, mode, run_dir, sut_jar, runner_jar, agent_jar, evomaster_jar):
    """Run one mode with the controller and EvoMaster in separate threads."""
    run_dir.mkdir(parents=True)
    controller_command = [args.java, "-Dem.muteSUT=true",
                          f"-Devomaster.instrumentation.jar.path={agent_jar}",
                          "-jar", str(runner_jar), str(args.controller_port),
                          str(args.controller_port + 1), str(sut_jar),
                          str(args.startup_timeout), args.java]
    em_command = [args.em_java or args.java, "-jar", str(evomaster_jar),
                  "--blackBox=false", f"--sutControllerPort={args.controller_port}",
                  f"--seed={seed}", "--stoppingCriterion=TIME",
                  f"--maxTime={args.max_time}",
                  f"--outputFolder={run_dir / 'tests'}",
                  "--writeStatistics=true",
                  f"--statisticsFile={run_dir / 'statistics.csv'}",
                  f"--instrumentMR_DYNAMODB={str(mode.instrument_dynamodb).lower()}",
                  f"--heuristicsForDynamoDb={str(mode.dynamodb_heuristics).lower()}",
                  f"--extractDynamoDbExecutionInfo={str(mode.extract_dynamodb_execution_info).lower()}",
                  f"--generateDynamoDbData={str(mode.generate_dynamodb_data).lower()}"]
    metadata = {"app": args.app, "cycle": cycle_number, "mode": mode.name, "seed": seed,
                "instrument_dynamodb": mode.instrument_dynamodb,
                "dynamodb_heuristics": mode.dynamodb_heuristics,
                "extract_dynamodb_execution_info": mode.extract_dynamodb_execution_info,
                "generate_dynamodb_data": mode.generate_dynamodb_data,
                "controller_command": controller_command, "evomaster_command": em_command}
    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    ready = threading.Event()
    stop = threading.Event()
    failures = []
    exit_codes = {}

    def controller_worker():
        """Launch the controller, signal readiness, and own its cleanup."""
        try:
            with (run_dir / "controller.log").open("wb") as log:
                process = subprocess.Popen(controller_command, cwd=args.dist, stdout=log,
                                           stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                           start_new_session=os.name != "nt")
                try:
                    deadline = time.monotonic() + args.startup_timeout
                    while not stop.is_set() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError(f"controller exited with code {process.returncode}; see controller.log")
                        if controller_available(args.controller_port):
                            ready.set()
                            break
                        stop.wait(0.25)
                    if not ready.is_set() and not stop.is_set():
                        raise RuntimeError("controller startup timed out; see controller.log")
                    while not stop.wait(0.25):
                        if process.poll() is not None:
                            raise RuntimeError(f"controller exited with code {process.returncode}; see controller.log")
                finally:
                    stop_process(process)
        except Exception as error:
            failures.append(str(error))
            stop.set()

    def evomaster_worker():
        """Wait for the controller, run EvoMaster, and record its exit code."""
        while not ready.is_set() and not stop.is_set():
            ready.wait(0.25)
        if stop.is_set():
            return
        try:
            with (run_dir / "evomaster.log").open("wb") as log:
                process = subprocess.Popen(em_command, cwd=run_dir, stdout=log,
                                           stderr=subprocess.STDOUT)
                try:
                    while process.poll() is None and not stop.wait(0.25):
                        pass
                    if stop.is_set() and process.poll() is None:
                        stop_process(process)
                    exit_codes["evomaster"] = process.wait()
                    if exit_codes["evomaster"] != 0 and not failures:
                        failures.append(f"EvoMaster exited with code {exit_codes['evomaster']}; see evomaster.log")
                finally:
                    stop.set()
        except Exception as error:
            failures.append(str(error))
            stop.set()

    controller = threading.Thread(target=controller_worker, name="sut-controller")
    evomaster = threading.Thread(target=evomaster_worker, name="evomaster")
    controller.start()
    evomaster.start()
    try:
        evomaster.join()
    except KeyboardInterrupt:
        stop.set()
        evomaster.join()
        raise
    finally:
        stop.set()
        controller.join()
    metadata["evomaster_exit_code"] = exit_codes.get("evomaster")
    metadata["error"] = "; ".join(failures) if failures else None
    metadata["warnings"] = controller_warnings(run_dir / "controller.log")
    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if failures:
        raise RuntimeError("; ".join(failures))
    return metadata["warnings"]


def main():
    """Validate artifacts and execute each three-mode comparison cycle."""
    args = parse_args()
    args.dist = args.dist.resolve()
    args.output_dir = args.output_dir.resolve()
    sut_jar = args.dist / f"{args.app}-sut.jar"
    runner_jar = args.dist / f"{args.app}-evomaster-runner.jar"
    agent_jar = args.dist / "evomaster-agent.jar"
    evomaster_jar = (args.evomaster_jar or args.dist / "evomaster.jar").resolve()
    for artifact in (sut_jar, runner_jar, agent_jar, evomaster_jar):
        if not artifact.is_file():
            raise SystemExit(f"Missing {artifact}")
    validate_java(args.java, "--java")
    validate_java(args.em_java or args.java, "--em-java" if args.em_java else "--java")
    validate_evomaster_jar(evomaster_jar)
    if controller_available(args.controller_port):
        raise SystemExit(f"Controller port {args.controller_port} is already in use")
    first_cycle = 1
    while (args.output_dir / args.app / f"cycle-{first_cycle:03d}").exists():
        first_cycle += 1
    for number in range(first_cycle, first_cycle + args.runs):
        cycle_dir = args.output_dir / args.app / f"cycle-{number:03d}"
        if cycle_dir.exists():
            raise SystemExit(f"Output directory already exists: {cycle_dir}")
    random_generator = random.SystemRandom()
    generated_seeds = set()
    for index, number in enumerate(range(first_cycle, first_cycle + args.runs), start=1):
        if args.seed is None:
            cycle_seed = random_generator.randrange(1, 2**63)
            while cycle_seed in generated_seeds:
                cycle_seed = random_generator.randrange(1, 2**63)
            generated_seeds.add(cycle_seed)
        else:
            cycle_seed = args.seed
        for mode in MODES:
            run_dir = args.output_dir / args.app / f"cycle-{number:03d}" / mode.name
            print(f"Running {args.app} cycle {index}/{args.runs}, {mode.name}, "
                  f"seed {cycle_seed}: {run_dir}", flush=True)
            try:
                warnings = run_once(args, number, cycle_seed, mode, run_dir, sut_jar, runner_jar,
                                    agent_jar, evomaster_jar)
            except RuntimeError as error:
                raise SystemExit(f"Cycle {index}, {mode.name} failed: {error}") from error
            for warning in warnings:
                print(f"Cycle {index}, {mode.name} warning: {warning}", flush=True)


if __name__ == "__main__":
    main()

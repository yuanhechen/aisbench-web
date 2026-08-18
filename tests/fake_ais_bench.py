#!/usr/bin/env python3
"""A deterministic stand-in for the real ais_bench CLI.

It accepts the same positional config plus --mode and --work-dir the worker passes, so the
worker's command line is exercised for real, and adds --scenario to choose the outcome.
"""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

SUMMARY_CSV = "dataset,version,metric,mode,job-model\ngsm8k,1d7fe4,accuracy,gen,87.50\n"
TOTAL_PROMPTS = 8


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--mode", default="all")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument(
        "--scenario",
        default=os.environ.get("FAKE_AIS_BENCH_SCENARIO", "success"),
        choices=("success", "fail", "sleep"),
    )
    return parser.parse_args(argv)


def run_success(work_dir: Path) -> int:
    for completed in (2, 5, TOTAL_PROMPTS):
        print(f"PROGRESS {completed}/{TOTAL_PROMPTS}", flush=True)
    summary = work_dir / "summary"
    summary.mkdir(parents=True, exist_ok=True)
    (summary / "summary_test.csv").write_text(SUMMARY_CSV, encoding="utf-8")
    print("AISBench finished", flush=True)
    return 0


def run_fail() -> int:
    print("ERROR: the model endpoint refused the request", file=sys.stderr, flush=True)
    return 3


def run_sleep() -> int:
    def terminate(_signum, _frame):
        print("received SIGTERM", flush=True)
        sys.exit(143)

    signal.signal(signal.SIGTERM, terminate)
    print(f"PROGRESS 0/{TOTAL_PROMPTS}", flush=True)
    while True:
        time.sleep(0.05)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.scenario == "success":
        return run_success(Path(args.work_dir))
    if args.scenario == "fail":
        return run_fail()
    return run_sleep()


if __name__ == "__main__":
    sys.exit(main())

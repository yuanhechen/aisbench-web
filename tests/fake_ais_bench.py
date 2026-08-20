#!/usr/bin/env python3
"""A deterministic stand-in for the real ais_bench CLI.

It accepts the same positional config plus --mode and --work-dir the worker passes, so the
worker's command line is exercised for real, and adds --scenario to choose the outcome.
A successful run reproduces the shapes the real CLI leaves behind: a timestamped run
directory, per-task status files under status_tmp (emptied and removed as the stages end),
one task log per dataset, and summary plus results files.
"""

import argparse
import json
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path

MODEL_ABBR = re.compile(r"abbr\s*=\s*['\"]([^'\"]+)['\"]")
DATASET_IMPORTS = re.compile(r"from\s+[\w.]+\s+import\s+\w+\s+as\s+ds_\d+")
IMPORT_MODULE = re.compile(r"from\s+([\w.]+)\s+import\s+\w+\s+as\s+ds_\d+")

TOTAL_PROMPTS = 8
SNAPSHOT_DELAY = float(os.environ.get("FAKE_AIS_BENCH_SNAPSHOT_DELAY", "0.05"))


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--mode", default="all")
    parser.add_argument("--work-dir", required=True)
    # Mirrors the real CLI: an option it accepts must not make the stand-in exit 2.
    parser.add_argument("--max-num-workers", type=int, default=1)
    parser.add_argument("--max-workers-per-gpu", type=int, default=1)
    parser.add_argument("--num-prompts", type=int)
    parser.add_argument("--num-warmups", type=int)
    parser.add_argument("--pressure-time", type=int)
    parser.add_argument("--pressure", action="store_true")
    parser.add_argument("--spec-decode", action="store_true")
    parser.add_argument("--merge-ds", action="store_true")
    parser.add_argument("--dump-eval-details", action="store_true")
    parser.add_argument("--dump-extract-rate", action="store_true")
    parser.add_argument(
        "--scenario",
        default=os.environ.get("FAKE_AIS_BENCH_SCENARIO", "success"),
        choices=("success", "fail", "sleep", "merge", "taskfail"),
    )
    return parser.parse_args(argv)


def datasets_from_config(config_path: str) -> list[str]:
    """The abbrs of the datasets the generated config imports, in the order it imports them.

    An explicit list wins: tests that do not care to lay out a config tree say it directly.
    """
    override = os.environ.get("FAKE_AIS_BENCH_DATASETS")
    if override:
        return [name.strip() for name in override.split(",") if name.strip()]
    source = Path(config_path).read_text(encoding="utf-8")
    if not DATASET_IMPORTS.search(source):
        return ["gsm8k"]
    # ...configs.datasets.<dir>.<config>: the directory is the abbr the fixtures declare.
    return [
        module.rsplit(".", 2)[-2] for module in IMPORT_MODULE.findall(source)
    ]


def model_abbr_from_config(config_path: str) -> str:
    source = Path(config_path).read_text(encoding="utf-8")
    match = MODEL_ABBR.search(source)
    return "job-model" if match is None else match.group(1)


def write_snapshot(status_dir: Path, task: str, state: dict) -> None:
    """Append one snapshot, exactly the way AISBench's write_status does."""
    path = status_dir / f"tmp_{task.replace('/', '_')}.json"
    existing = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    existing.append(state)
    path.write_text(json.dumps(existing), encoding="utf-8")


def run_task(status_dir: Path, run_dir: Path, model_abbr: str, dataset: str) -> None:
    """One dataset's inference: the snapshot sequence a real task writes, then its log."""
    task = f"{model_abbr}/{dataset}"
    started = time.time()
    log_dir = run_dir / "logs" / "infer" / model_abbr
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{dataset}.out"
    base = {
        "task_name": task,
        "process_id": os.getpid(),
        "task_log_path": f"logs/infer/{model_abbr}/{dataset}.out",
    }
    # The TasksMonitor clears files as it reads them; a bare [] is a legitimate sight.
    (status_dir / f"tmp_{model_abbr}_{dataset}.json").write_text("[]", encoding="utf-8")
    time.sleep(SNAPSHOT_DELAY)
    for status, finished, description, counters in (
        ("start", 0, "Infer progress", None),
        ("load model", 0, "Infer progress", None),
        (
            "inferencing",
            2,
            "[41.7 it/s]",
            {"POST": 2, "RECV": 2, "FINISH": 2, "FAIL": 0},
        ),
        (
            "inferencing",
            5,
            "[38.2 it/s]",
            {"POST": 5, "RECV": 5, "FINISH": 5, "FAIL": 0},
        ),
        (
            "inferencing",
            TOTAL_PROMPTS,
            "[40.0 it/s]",
            {"POST": 8, "RECV": 8, "FINISH": 8, "FAIL": 0},
        ),
        ("write cache", TOTAL_PROMPTS, "Infer progress", None),
        ("finish", TOTAL_PROMPTS, "Infer progress", None),
    ):
        write_snapshot(
            status_dir,
            task,
            {
                **base,
                "start_time": started,
                "status": status,
                "finish_count": finished,
                "total_count": TOTAL_PROMPTS,
                "progress_description": description,
                **({"other_kwargs": counters} if counters else {}),
            },
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[fake] {dataset} {status} {finished}/{TOTAL_PROMPTS}\n")
        time.sleep(SNAPSHOT_DELAY)


def write_results(run_dir: Path, model_abbr: str, datasets: list[str]) -> None:
    summary = run_dir / "summary"
    summary.mkdir(parents=True, exist_ok=True)
    lines = ["dataset,version,metric,mode," + model_abbr]
    for dataset in datasets:
        lines.append(f"{dataset},1d7fe4,accuracy,gen,87.50 (7/{TOTAL_PROMPTS})")
    (summary / "summary_test.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    results_dir = run_dir / "results" / model_abbr
    results_dir.mkdir(parents=True, exist_ok=True)
    for dataset in datasets:
        (results_dir / f"{dataset}.json").write_text(
            json.dumps(
                {
                    "accuracy": 87.5,
                    "correct_count": 7,
                    "total_count": TOTAL_PROMPTS,
                }
            ),
            encoding="utf-8",
        )


def run_success(work_dir: Path, datasets: list[str]) -> int:
    model_abbr = "job-model"
    run_dir = work_dir / time.strftime("%Y%m%d_%H%M%S")
    status_dir = run_dir / "status_tmp"
    status_dir.mkdir(parents=True, exist_ok=True)
    print("Starting inference tasks...", flush=True)
    for position, dataset in enumerate(datasets):
        run_task(status_dir, run_dir, model_abbr, dataset)
        print(
            f"Monitoring tasks progress: {int((position + 1) / len(datasets) * 100)}%"
            f"|{'#' * (position + 1)}| {position + 1}/{len(datasets)}",
            flush=True,
        )
        print(f"PROGRESS {(position + 1) * TOTAL_PROMPTS}/{len(datasets) * TOTAL_PROMPTS}", flush=True)
    shutil.rmtree(status_dir)
    print("Inference tasks completed.", flush=True)

    print("Starting evaluation tasks...", flush=True)
    status_dir.mkdir(parents=True, exist_ok=True)
    for dataset in datasets:
        task = f"{model_abbr}/{dataset}"
        write_snapshot(
            status_dir,
            task,
            {
                "task_name": task,
                "process_id": os.getpid(),
                "start_time": time.time(),
                "status": "start",
                "task_log_path": f"logs/eval/{model_abbr}/{dataset}.out",
            },
        )
        write_snapshot(
            status_dir,
            task,
            {
                "task_name": task,
                "process_id": os.getpid(),
                "start_time": time.time(),
                "status": "finish",
                "progress_description": "Processing predictions",
                "task_log_path": f"logs/eval/{model_abbr}/{dataset}.out",
            },
        )
        time.sleep(SNAPSHOT_DELAY)
    shutil.rmtree(status_dir)
    print("Evaluation tasks completed.", flush=True)

    write_results(run_dir, model_abbr, datasets)
    print("AISBench finished", flush=True)
    return 0


def run_merge(work_dir: Path) -> int:
    """Perf-style run: every dataset collapses into one task named after the class."""
    run_dir = work_dir / time.strftime("%Y%m%d_%H%M%S")
    status_dir = run_dir / "status_tmp"
    status_dir.mkdir(parents=True, exist_ok=True)
    run_task(status_dir, run_dir, "job-model", "customdataset")
    shutil.rmtree(status_dir)
    print("Inference tasks completed.", flush=True)
    write_results(run_dir, "job-model", ["gsm8k", "mmlu"])
    return 0


def run_fail() -> int:
    print("ERROR: the model endpoint refused the request", file=sys.stderr, flush=True)
    return 3


def run_task_fail(work_dir: Path, datasets: list[str]) -> int:
    """A task that dies, the workflow that carries on: exit 0, dash-only summary.

    This is the shape a refused endpoint leaves behind — the task fails, AISBench still
    runs the evaluation and writes a summary row of dashes per dataset.
    """
    run_dir = work_dir / time.strftime("%Y%m%d_%H%M%S")
    print("Starting inference tasks...", flush=True)
    for dataset in datasets:
        print(
            f"OpenICLApiInferjob-model/{dataset} failed with code 1, see",
            flush=True,
        )
    print("Inference tasks completed.", flush=True)
    print("Starting evaluation tasks...", flush=True)
    print("Evaluation tasks completed.", flush=True)
    summary = run_dir / "summary"
    summary.mkdir(parents=True, exist_ok=True)
    lines = ["dataset,version,metric,mode,job-model"]
    lines += [f"{dataset},-,-,-,-" for dataset in datasets]
    (summary / "summary_test.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("AISBench finished", flush=True)
    return 0


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
        return run_success(Path(args.work_dir), datasets_from_config(args.config))
    if args.scenario == "merge":
        return run_merge(Path(args.work_dir))
    if args.scenario == "taskfail":
        return run_task_fail(Path(args.work_dir), datasets_from_config(args.config))
    if args.scenario == "fail":
        return run_fail()
    return run_sleep()


if __name__ == "__main__":
    sys.exit(main())

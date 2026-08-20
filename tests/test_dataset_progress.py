"""The per-dataset progress reader, against the file shapes AISBench actually writes."""

import json
from pathlib import Path

from aisbench_web.jobs.dataset_progress import (
    PHASE_EVALUATING,
    PHASE_FAILED,
    PHASE_FINISHED,
    PHASE_INFERRING,
    PHASE_LOADING,
    PHASE_QUEUED,
    DatasetProgressCollector,
    DatasetStatus,
    apply_stage,
    mark_failed,
    parse_failed_dataset,
    parse_rate,
    parse_stage_line,
    read_last_snapshot,
    scan_status_dir,
    split_task_name,
)

MODEL = "job-model"


def make_run(tmp_path: Path, run_name: str = "20260819_000000") -> Path:
    status_dir = tmp_path / "outputs" / run_name / "status_tmp"
    status_dir.mkdir(parents=True)
    return status_dir


def write_status(status_dir: Path, task: str, snapshots: list[dict]) -> Path:
    path = status_dir / f"tmp_{task.replace('/', '_')}.json"
    path.write_text(json.dumps(snapshots), encoding="utf-8")
    return path


def snapshot(dataset: str, **overrides) -> dict:
    base = {
        "task_name": f"{MODEL}/{dataset}",
        "process_id": 123,
        "start_time": 1755580800.0,
        "status": "inferencing",
        "finish_count": 3,
        "total_count": 8,
        "progress_description": "[38.2 it/s]",
        "other_kwargs": {"POST": 5, "RECV": 4, "FINISH": 3, "FAIL": 0},
        "task_log_path": f"logs/infer/{MODEL}/{dataset}.out",
    }
    return {**base, **overrides}


# --- one status file ----------------------------------------------------------


def test_the_newest_snapshot_of_a_small_file_is_read_whole(tmp_path: Path) -> None:
    path = make_run(tmp_path).parent / "tmp_job-model_gsm8k.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([snapshot("gsm8k", finish_count=1), snapshot("gsm8k", finish_count=7)]),
        encoding="utf-8",
    )

    assert read_last_snapshot(path)["finish_count"] == 7


def test_an_empty_file_is_no_news_not_no_progress(tmp_path: Path) -> None:
    """The TasksMonitor clears files as it consumes them; [] means 'nothing new'."""
    path = tmp_path / "tmp_job-model_gsm8k.json"
    path.write_text("[]", encoding="utf-8")

    assert read_last_snapshot(path) is None


def test_a_torn_write_is_no_news(tmp_path: Path) -> None:
    path = tmp_path / "tmp_job-model_gsm8k.json"
    body = json.dumps([snapshot("gsm8k")])
    path.write_text(body[: len(body) // 2], encoding="utf-8")

    assert read_last_snapshot(path) is None


def test_a_long_running_tasks_file_is_read_from_the_tail(tmp_path: Path) -> None:
    """The array grows twice a second for hours; parsing it whole would parse megabytes."""
    path = tmp_path / "tmp_job-model_gsm8k.json"
    filler = [snapshot("gsm8k", finish_count=index) for index in range(20_000)]
    filler[-1] = snapshot("gsm8k", finish_count=19_999, status="finish")
    # A brace inside a string value is what a backwards scan would trip over.
    filler[-1]["progress_description"] = "[1 it/s} and a } in a string"
    path.write_text(json.dumps(filler), encoding="utf-8")
    assert path.stat().st_size > 512 * 1024

    newest = read_last_snapshot(path)

    assert newest is not None
    assert newest["status"] == "finish"
    assert newest["progress_description"].endswith("in a string")


# --- names and phases ---------------------------------------------------------


def test_the_dataset_part_is_split_off_the_model_prefix() -> None:
    assert split_task_name(f"{MODEL}/ARC-e", MODEL) == "ARC-e"
    assert split_task_name("another-model/gsm8k", MODEL) is None
    # A dataset part that still holds a separator is a merged or foreign task.
    assert split_task_name(f"{MODEL}/customdataset/extra", MODEL) is None
    assert split_task_name(f"{MODEL}/", MODEL) is None


def test_rates_come_out_of_the_progress_description() -> None:
    assert parse_rate("[38.2 it/s]") == "38.2 it/s"
    assert parse_rate("Infer progress") is None
    assert parse_rate(None) is None


def test_failure_lines_name_the_dataset_that_failed() -> None:
    line = f"{MODEL}/ARC-e failed with code 1, see\n outputs/x/logs/infer/job-model/ARC-e.out"
    assert parse_failed_dataset(line, MODEL) == "ARC-e"
    assert parse_failed_dataset("something else entirely", MODEL) is None


# --- scanning a run directory -------------------------------------------------


def test_each_dataset_reports_its_own_progress(tmp_path: Path) -> None:
    status_dir = make_run(tmp_path)
    write_status(status_dir, f"{MODEL}/ARC-e", [snapshot("ARC-e", status="finish", finish_count=8)])
    write_status(status_dir, f"{MODEL}/gsm8k", [snapshot("gsm8k", finish_count=3)])
    output_dir = tmp_path / "outputs"

    states = scan_status_dir(output_dir, MODEL, {})

    assert states["ARC-e"].phase == PHASE_FINISHED
    assert (states["ARC-e"].completed, states["ARC-e"].total) == (8, 8)
    assert states["gsm8k"].phase == PHASE_INFERRING
    assert states["gsm8k"].rate == "38.2 it/s"
    assert states["gsm8k"].counters == {"POST": 5, "RECV": 4, "FINISH": 3, "FAIL": 0}
    # The task log is addressed relative to the job directory, ts run included.
    assert states["gsm8k"].log_path == "outputs/20260819_000000/logs/infer/job-model/gsm8k.out"
    assert states["gsm8k"].started_at is not None


def test_a_cleared_file_keeps_the_last_known_state(tmp_path: Path) -> None:
    status_dir = make_run(tmp_path)
    write_status(status_dir, f"{MODEL}/gsm8k", [snapshot("gsm8k")])
    previous = scan_status_dir(tmp_path / "outputs", MODEL, {})
    write_status(status_dir, f"{MODEL}/gsm8k", [])  # the monitor just consumed it

    states = scan_status_dir(tmp_path / "outputs", MODEL, previous)

    assert states["gsm8k"].completed == 3


def test_a_finished_dataset_moves_to_evaluating_when_its_task_reappears(tmp_path: Path):
    status_dir = make_run(tmp_path, "20260819_000001")
    previous = {
        "gsm8k": DatasetStatus(
            dataset="gsm8k", phase=PHASE_FINISHED, completed=8, total=8
        )
    }
    # The eval stage recreates the same task names after the infer files were removed.
    write_status(
        status_dir,
        f"{MODEL}/gsm8k",
        [snapshot("gsm8k", status="start", finish_count=None, progress_description="")],
    )

    states = scan_status_dir(tmp_path / "outputs", MODEL, previous)

    assert states["gsm8k"].phase == PHASE_EVALUATING
    # The infer counts stay on display through the eval stage.
    assert (states["gsm8k"].completed, states["gsm8k"].total) == (8, 8)


def test_a_merged_task_is_one_row_under_the_class_name_aisbench_used(tmp_path: Path) -> None:
    """Perf and --merge-ds collapse the datasets into one task; the row shows under that
    name and the API appends it after the configured datasets."""
    status_dir = make_run(tmp_path)
    write_status(
        status_dir,
        "job-model/customdataset",
        [snapshot("customdataset", task_name=f"{MODEL}/customdataset")],
    )

    states = scan_status_dir(tmp_path / "outputs", MODEL, {})

    assert states["customdataset"].phase == PHASE_INFERRING
    assert states["customdataset"].dataset == "customdataset"


def test_stage_lines_settle_rows_the_monitor_may_have_eaten_first() -> None:
    states = {
        "ARC-e": DatasetStatus(dataset="ARC-e", phase=PHASE_INFERRING, total=8, completed=5),
        "gsm8k": DatasetStatus(dataset="gsm8k", phase=PHASE_LOADING, total=8),
    }

    apply_stage(states, parse_stage_line("Inference tasks completed."))

    assert states["ARC-e"].phase == PHASE_FINISHED
    assert states["ARC-e"].completed == 8
    assert states["gsm8k"].phase == PHASE_FINISHED


def test_a_failed_line_marks_only_that_dataset() -> None:
    states = {
        "ARC-e": DatasetStatus(dataset="ARC-e", phase=PHASE_INFERRING),
        "gsm8k": DatasetStatus(dataset="gsm8k", phase=PHASE_INFERRING),
    }

    mark_failed(states, "ARC-e")

    assert states["ARC-e"].phase == PHASE_FAILED
    assert states["gsm8k"].phase == PHASE_INFERRING


def test_no_status_directory_leaves_the_previous_states_alone(tmp_path: Path) -> None:
    previous = {"gsm8k": DatasetStatus(dataset="gsm8k", phase=PHASE_INFERRING)}

    assert scan_status_dir(tmp_path / "outputs", MODEL, previous) is previous


# --- the collector the worker drives ------------------------------------------


def test_the_collector_seeds_queued_rows_and_finishes_them_on_success(tmp_path: Path) -> None:
    collector = DatasetProgressCollector(
        model_abbr=MODEL,
        output_dir=tmp_path / "outputs",
        # A scan interval long enough that only the forced scans run in this test.
        scan_interval=60.0,
    )
    collector.states = {
        "ARC-e": DatasetStatus(dataset="ARC-e", phase=PHASE_QUEUED),
        "gsm8k": DatasetStatus(dataset="gsm8k", phase=PHASE_QUEUED),
    }

    status_dir = make_run(tmp_path, "20260819_000002")
    write_status(status_dir, f"{MODEL}/gsm8k", [snapshot("gsm8k")])
    assert collector.scan(force=True) is True
    assert collector.states["gsm8k"].phase == PHASE_INFERRING
    assert collector.states["ARC-e"].phase == PHASE_QUEUED  # seeded, not yet started

    collector.consume_lines(["Inference tasks completed."])
    collector.finish(succeeded=True)

    assert all(state.phase == PHASE_FINISHED for state in collector.states.values())


def test_a_failed_run_leaves_the_rows_where_they_stopped(tmp_path: Path) -> None:
    collector = DatasetProgressCollector(
        model_abbr=MODEL, output_dir=tmp_path / "outputs", scan_interval=60.0
    )
    collector.states = {
        "gsm8k": DatasetStatus(dataset="gsm8k", phase=PHASE_INFERRING, completed=3, total=8)
    }

    collector.consume_lines([f"{MODEL}/gsm8k failed with code 1, see"])
    collector.finish(succeeded=False)

    assert collector.states["gsm8k"].phase == PHASE_FAILED

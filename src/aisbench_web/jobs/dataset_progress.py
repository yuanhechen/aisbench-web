"""Per-dataset progress read from the status files AISBench writes while it runs.

AISBench gives every (model, dataset) pair its own task, and every task appends a snapshot
to ``<run>/status_tmp/tmp_<task>.json`` twice a second. The TasksMonitor process clears those
files again as it consumes them, so a read can legitimately see an empty list or a half-written
file — both mean "no news since last time", never "no progress". Snapshots the monitor eats
before they are read here are covered instead by the stage lines the main process prints
("Inference tasks completed.") and, ultimately, by the process exit code.
"""

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_DIRNAME = "status_tmp"

# The phases a dataset moves through, as the UI names them.
PHASE_QUEUED = "queued"
PHASE_LOADING = "loading"
PHASE_INFERRING = "inferring"
PHASE_WRITING_CACHE = "writing_cache"
PHASE_EVALUATING = "evaluating"
PHASE_FINISHED = "finished"
PHASE_FAILED = "failed"

ACTIVE_PHASES = (PHASE_LOADING, PHASE_INFERRING, PHASE_WRITING_CACHE, PHASE_EVALUATING)

RATE_PATTERN = re.compile(r"\[([\d.]+\s*(?:it/s|s))\]")
CORRECT_TOTAL_PATTERN = re.compile(r"\((\d+)\s*/\s*(\d+)\)")

# Stage lines the AISBench main process prints; the worker hears them through the process log.
STAGE_INFER_STARTED = "infer_started"
STAGE_INFER_COMPLETED = "infer_completed"
STAGE_EVAL_STARTED = "eval_started"
STAGE_EVAL_COMPLETED = "eval_completed"
STAGE_LINES = {
    "Starting inference tasks...": STAGE_INFER_STARTED,
    "Inference tasks completed.": STAGE_INFER_COMPLETED,
    "Starting evaluation tasks...": STAGE_EVAL_STARTED,
    "Evaluation tasks completed.": STAGE_EVAL_COMPLETED,
}

# The snapshot array grows twice a second for as long as the task runs, so a long job's file
# reaches tens of megabytes. Small files are parsed whole; large ones only through a tail
# window, which always holds thousands of snapshots.
WHOLE_READ_LIMIT = 512 * 1024
TAIL_READ_SIZE = 128 * 1024


@dataclass
class DatasetStatus:
    """One dataset's latest known state, shaped for the job_dataset_progress table."""

    dataset: str
    phase: str
    raw_status: str | None = None
    completed: int | None = None
    total: int | None = None
    rate: str | None = None
    counters: dict[str, Any] | None = None
    # Relative to the job directory, so the ts-run directory changing underneath cannot
    # address a file the job does not own.
    log_path: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    # Filled once the run succeeded and the results were parsed.
    metrics: dict[str, dict[str, Any]] | None = None
    correct_count: int | None = None
    total_count: int | None = None


@dataclass
class DatasetProgressCollector:
    """Track dataset states for one running job: scan status files, hear stage lines."""

    model_abbr: str
    output_dir: Path
    states: dict[str, DatasetStatus] = field(default_factory=dict)
    #: Set by seeding and by any change the scans and stage lines make; the worker stores
    #: the states only while this is set, so a quiet run costs no writes.
    dirty: bool = False
    scan_interval: float = 0.5
    _last_scan: float = 0.0

    def seed(self, dataset: str) -> None:
        """A row for a chosen dataset before AISBench names its task."""
        if dataset and dataset not in self.states:
            self.states[dataset] = DatasetStatus(dataset=dataset, phase=PHASE_QUEUED)
            self.dirty = True

    def start(self) -> None:
        """The process is up: queued rows are now starting, not waiting their turn.

        AISBench's interpreter takes tens of seconds to import before its first task
        writes anything; a row that still said "queued" all that time reads as stuck.
        """
        for name, state in list(self.states.items()):
            if state.phase == PHASE_QUEUED:
                self.states[name] = replace(state, phase=PHASE_LOADING)
                self.dirty = True

    def consume_lines(self, lines: list[str]) -> bool:
        """Apply the stage and failure lines the process log produced since last time."""
        changed = False
        for line in lines:
            stage = parse_stage_line(line)
            if stage is not None and apply_stage(self.states, stage):
                changed = True
            failed = parse_failed_dataset(line, self.model_abbr)
            if failed is not None and mark_failed(self.states, failed):
                changed = True
        self.dirty = self.dirty or changed
        return changed

    def scan(self, *, force: bool = False) -> bool:
        """Refresh from status_tmp, at most once per interval.

        Returns True when the stored states may have changed.
        """
        now = time.monotonic()
        if not force and now - self._last_scan < self.scan_interval:
            return False
        self._last_scan = now
        scanned = scan_status_dir(self.output_dir, self.model_abbr, self.states)
        if scanned is self.states:
            return False
        self.states = scanned
        self.dirty = True
        return True

    def finish(self, *, succeeded: bool) -> None:
        """Settle the rows once the process is gone; missed finish snapshots land here."""
        self.scan(force=True)
        if not succeeded:
            return
        for name, state in list(self.states.items()):
            if state.phase != PHASE_FAILED:
                self.states[name] = replace(
                    state,
                    phase=PHASE_FINISHED,
                    completed=state.total if state.total is not None else state.completed,
                    rate=None,
                )


def read_last_snapshot(path: Path) -> dict[str, Any] | None:
    """The newest snapshot in a status file, or None when there is no readable news.

    Empty lists (the monitor just consumed the file) and torn writes parse as no news.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= WHOLE_READ_LIMIT:
                return _last_of(json.loads(handle.read().decode("utf-8")))
            handle.seek(max(0, size - TAIL_READ_SIZE))
            return _last_of_array_tail(handle.read())
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError and invalid UTF-8 in a torn write.
        return None


def _last_of(parsed: Any) -> dict[str, Any] | None:
    if not isinstance(parsed, list) or not parsed:
        return None
    newest = parsed[-1]
    return newest if isinstance(newest, dict) else None


def _last_of_array_tail(tail: bytes) -> dict[str, Any] | None:
    """The last element of a JSON array from its truncated tail.

    The chunk can start mid-element — even mid-string — so a forward scan of it can never
    be sure of its sync. Walking backwards cannot lose it: the file ends with the array's
    closing bracket, outside any string, so the depth count is in sync from the first byte
    backwards. Strings are skipped whole, honouring escaped quotes; the element's opening
    brace is where the depth returns to zero.
    """
    text = tail.decode("utf-8", errors="replace")
    index = len(text) - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    if index < 0 or text[index] != "]":
        return None
    index -= 1
    depth = 0
    while index >= 0:
        char = text[index]
        if char == '"':
            index = _string_start(text, index) - 1
            continue
        if char in "}]":
            depth += 1
        elif char in "{[":
            depth -= 1
            if depth <= 0:
                break
        index -= 1
    if index < 0:
        return None
    try:
        element, end = json.JSONDecoder().raw_decode(text, index)
    except ValueError:
        return None
    if text[end:].lstrip()[:1] != "]":
        return None
    return element if isinstance(element, dict) else None


def _string_start(text: str, closing_quote: int) -> int:
    """Where the string closed at `closing_quote` began, honouring escaped quotes."""
    index = closing_quote - 1
    while index >= 0:
        if text[index] == '"':
            backslashes = 0
            probe = index - 1
            while probe >= 0 and text[probe] == "\\":
                backslashes += 1
                probe -= 1
            if backslashes % 2 == 0:
                return index
        index -= 1
    return 0


def split_task_name(task_name: str, model_abbr: str) -> str | None:
    """The dataset part of "model/dataset", or None when the task is not this model's.

    A dataset part that is empty or still holds a separator belongs to a merged or foreign
    task the UI shows as a single row under its raw name.
    """
    prefix = f"{model_abbr}/"
    if not task_name.startswith(prefix):
        return None
    dataset = task_name[len(prefix) :]
    if not dataset or "/" in dataset:
        return None
    return dataset


def parse_rate(description: str | None) -> str | None:
    if not description:
        return None
    match = RATE_PATTERN.search(description)
    return match.group(1) if match else None


def phase_from_status(raw_status: Any) -> str:
    if not isinstance(raw_status, str):
        return PHASE_INFERRING
    return _PHASE_BY_STATUS.get(raw_status.strip().lower(), PHASE_INFERRING)


_PHASE_BY_STATUS = {
    "start": PHASE_LOADING,
    "load model": PHASE_LOADING,
    "warmup": PHASE_LOADING,
    "inferencing": PHASE_INFERRING,
    "warmup finished": PHASE_INFERRING,
    "write cache": PHASE_WRITING_CACHE,
    "finish": PHASE_FINISHED,
    "error": PHASE_FAILED,
    "warmup failed": PHASE_FAILED,
}


def parse_stage_line(line: str) -> str | None:
    stripped = line.strip()
    for text, stage in STAGE_LINES.items():
        if text in stripped:
            return stage
    return None


def parse_failed_dataset(line: str, model_abbr: str) -> str | None:
    """The dataset in a "<task> failed with code <n>" line, when the line is one of ours."""
    if "failed with code" not in line:
        return None
    prefix = f"{model_abbr}/"
    start = line.find(prefix)
    if start < 0:
        return None
    remainder = line[start + len(prefix) :].split()
    if not remainder:
        return None
    dataset = remainder[0].rstrip(",;")
    return dataset or None


def apply_stage(states: dict[str, DatasetStatus], stage: str) -> None:
    """Settle rows from stage lines, covering finish snapshots the monitor consumed first."""
    settles: tuple[str, ...]
    if stage == STAGE_INFER_COMPLETED:
        settles = ACTIVE_PHASES
    elif stage == STAGE_EVAL_COMPLETED:
        settles = (PHASE_EVALUATING,)
    else:
        return False
    changed = False
    for name, state in list(states.items()):
        if state.phase in settles:
            states[name] = replace(
                state,
                phase=PHASE_FINISHED,
                completed=state.total if state.total is not None else state.completed,
                rate=None,
            )
            changed = True
    return changed


def mark_failed(states: dict[str, DatasetStatus], dataset: str) -> bool:
    state = states.get(dataset)
    if state is None or state.phase in (PHASE_FAILED, PHASE_FINISHED):
        return False
    states[dataset] = replace(state, phase=PHASE_FAILED, rate=None)
    return True


def scan_status_dir(
    output_dir: Path, model_abbr: str, previous: dict[str, DatasetStatus]
) -> dict[str, DatasetStatus]:
    """Fold the newest status_tmp snapshots into the previous states.

    Tasks whose file vanished (their stage finished and removed the directory) keep their
    last known state; the eval stage recreates the same task names, and a dataset that was
    already finished therefore moves to evaluating rather than backwards.
    """
    status_dir = _newest_status_dir(output_dir)
    if status_dir is None:
        return previous
    states = dict(previous)
    for path in sorted(status_dir.glob("tmp_*.json")):
        snapshot = read_last_snapshot(path)
        if snapshot is None:
            continue
        task_name = snapshot.get("task_name")
        if not isinstance(task_name, str) or not task_name:
            continue
        dataset = split_task_name(task_name, model_abbr)
        if dataset is None:
            # A merged run names its task after the dataset class; a foreign task is not
            # ours to attribute. Either way the row shows under the name AISBench used.
            dataset = task_name.split("/")[-1] or task_name
        phase = phase_from_status(snapshot.get("status"))
        previous_state = states.get(dataset)
        if (
            previous_state is not None
            and previous_state.phase == PHASE_FINISHED
            and phase in (PHASE_LOADING, PHASE_INFERRING)
        ):
            phase = PHASE_EVALUATING
        log_path = _log_path(output_dir, status_dir, snapshot.get("task_log_path"))
        states[dataset] = DatasetStatus(
            dataset=dataset,
            phase=phase,
            raw_status=snapshot.get("status"),
            completed=_keep_previous_int(snapshot.get("finish_count"), previous_state, "completed"),
            total=_keep_previous_int(snapshot.get("total_count"), previous_state, "total"),
            rate=parse_rate(snapshot.get("progress_description")),
            counters=_counters(snapshot.get("other_kwargs")),
            log_path=log_path if log_path is not None else (
                previous_state.log_path if previous_state is not None else None
            ),
            started_at=(
                previous_state.started_at
                if previous_state is not None and previous_state.started_at
                else _iso(snapshot.get("start_time"))
            ),
            updated_at=_now_iso(),
            metrics=previous_state.metrics if previous_state is not None else None,
            correct_count=(
                previous_state.correct_count if previous_state is not None else None
            ),
            total_count=previous_state.total_count if previous_state is not None else None,
        )
    return states


def _newest_status_dir(output_dir: Path) -> Path | None:
    """AISBench writes each run into a timestamped directory; only the newest one is live."""
    candidates = list(output_dir.glob(f"*/{STATUS_DIRNAME}"))
    if not candidates and (output_dir / STATUS_DIRNAME).is_dir():
        candidates.append(output_dir / STATUS_DIRNAME)
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    except OSError:
        return None


def _log_path(output_dir: Path, status_dir: Path, task_log_path: Any) -> str | None:
    """The task log relative to the job directory: outputs/<ts>/<snapshot's path>."""
    if not isinstance(task_log_path, str) or not task_log_path:
        return None
    relative = status_dir.parent.relative_to(output_dir.parent)
    return (relative / task_log_path).as_posix()


def _keep_previous_int(
    value: Any, previous: DatasetStatus | None, attribute: str
) -> int | None:
    """Eval-stage snapshots carry no counts; the infer counts stay on display."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if previous is None:
        return None
    return getattr(previous, attribute)


def _counters(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _iso(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

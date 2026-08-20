import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from aisbench_web.db import Database
from aisbench_web.jobs.dataset_progress import DatasetStatus
from aisbench_web.jobs.states import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    JobStatus,
    require_transition,
)

CONFIG_FILENAME = "generated_config.py"
LOG_FILENAME = "process.log"
OUTPUTS_DIRNAME = "outputs"
UPDATABLE_COLUMNS = ("pid", "exit_code", "error_code", "error_message")


def dataset_entries(snapshot: dict) -> list[dict]:
    """Datasets of a job snapshot. Jobs created before multi-dataset support stored one
    entry at the top level; read both shapes the same way."""
    entries = snapshot.get("datasets")
    if isinstance(entries, list) and entries:
        return [entry for entry in entries if isinstance(entry, dict)]
    return [snapshot]

SELECTED_COLUMNS = (
    "id, owner_id, name, model_endpoint_id, dataset_id, mode, status, model_snapshot_json, "
    "dataset_snapshot_json, parameters_json, config_path, output_dir, log_path, pid, "
    "exit_code, error_code, error_message, created_at, started_at, finished_at, "
    "progress_completed, progress_total"
)


@dataclass(frozen=True)
class StoredMetric:
    key: str
    value: float | None
    text_value: str | None
    unit: str | None


@dataclass(frozen=True)
class StoredArtifact:
    id: str
    job_id: str
    kind: str
    relative_path: str
    content_type: str


@dataclass(frozen=True)
class StoredDatasetProgress:
    dataset: str
    phase: str
    raw_status: str | None
    completed: int | None
    total: int | None
    rate: str | None
    counters: dict | None
    log_path: str | None
    metrics: dict | None
    correct_count: int | None
    total_count: int | None
    started_at: str | None
    updated_at: str


@dataclass(frozen=True)
class Job:
    id: str
    owner_id: str
    name: str
    model_endpoint_id: str
    dataset_id: str
    mode: str
    status: str
    parameters: dict
    model_snapshot: dict
    dataset_snapshot: dict
    config_path: str
    output_dir: str
    log_path: str
    pid: int | None
    exit_code: int | None
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    progress_completed: int | None = None
    progress_total: int | None = None


class JobRepository:
    """Owner-scoped job persistence plus the shared FIFO claim."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        owner_id: str,
        model_endpoint_id: str,
        dataset_id: str,
        mode: str,
        parameters: dict,
        model_snapshot: dict,
        dataset_snapshot: dict,
        name: str = "",
        now: datetime | None = None,
    ) -> Job:
        job_id = str(uuid4())
        timestamp = self._utc_now(now).isoformat()
        # Paths are stored relative to the jobs directory so no stored value can address a file
        # outside it, whatever a later download endpoint is asked for.
        job = Job(
            id=job_id,
            owner_id=owner_id,
            name=name,
            model_endpoint_id=model_endpoint_id,
            dataset_id=dataset_id,
            mode=mode,
            status=JobStatus.QUEUED.value,
            parameters=parameters,
            model_snapshot=model_snapshot,
            dataset_snapshot=dataset_snapshot,
            config_path=f"{job_id}/{CONFIG_FILENAME}",
            output_dir=f"{job_id}/{OUTPUTS_DIRNAME}",
            log_path=f"{job_id}/{LOG_FILENAME}",
            pid=None,
            exit_code=None,
            error_code=None,
            error_message=None,
            created_at=timestamp,
            started_at=None,
            finished_at=None,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                  id, owner_id, name, model_endpoint_id, dataset_id, mode, status,
                  model_snapshot_json, dataset_snapshot_json, parameters_json,
                  config_path, output_dir, log_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.owner_id,
                    job.name,
                    job.model_endpoint_id,
                    job.dataset_id,
                    job.mode,
                    job.status,
                    json.dumps(model_snapshot, ensure_ascii=False, sort_keys=True),
                    json.dumps(dataset_snapshot, ensure_ascii=False, sort_keys=True),
                    json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                    job.config_path,
                    job.output_dir,
                    job.log_path,
                    timestamp,
                    timestamp,
                ),
            )
        return job

    def replace_metrics(
        self,
        job_id: str,
        metrics: dict[str, tuple[float | None, str | None, str | None]],
    ) -> None:
        """Metrics are derived from the job's own output, so a re-parse replaces them wholly."""
        with self.database.connect() as connection:
            connection.execute("DELETE FROM job_metrics WHERE job_id = ?", (job_id,))
            connection.executemany(
                """
                INSERT INTO job_metrics (job_id, key, value, text_value, unit)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (job_id, key, value, text_value, unit)
                    for key, (value, text_value, unit) in metrics.items()
                ],
            )

    def replace_artifacts(self, job_id: str, artifacts: list[tuple[str, str, str]]) -> None:
        timestamp = self._now()
        with self.database.connect() as connection:
            connection.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
            connection.executemany(
                """
                INSERT INTO artifacts (id, job_id, kind, relative_path, content_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (str(uuid4()), job_id, kind, relative_path, content_type, timestamp)
                    for kind, relative_path, content_type in artifacts
                ],
            )

    def list_metrics_for_owner(self, job_id: str, owner_id: str) -> list[StoredMetric]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT key, value, text_value, unit
                FROM job_metrics
                JOIN jobs ON jobs.id = job_metrics.job_id
                WHERE job_metrics.job_id = ? AND jobs.owner_id = ?
                ORDER BY key
                """,
                (job_id, owner_id),
            ).fetchall()
        return [StoredMetric(**dict(row)) for row in rows]

    def list_artifacts_for_owner(self, job_id: str, owner_id: str) -> list[StoredArtifact]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT artifacts.id, artifacts.job_id, kind, relative_path, content_type
                FROM artifacts
                JOIN jobs ON jobs.id = artifacts.job_id
                WHERE artifacts.job_id = ? AND jobs.owner_id = ?
                ORDER BY relative_path
                """,
                (job_id, owner_id),
            ).fetchall()
        return [StoredArtifact(**dict(row)) for row in rows]

    def get_artifact_for_owner(
        self,
        job_id: str,
        artifact_id: str,
        owner_id: str,
    ) -> StoredArtifact | None:
        """An artifact is addressed only by ID; the stored path is never accepted from a client."""
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT artifacts.id, artifacts.job_id, kind, relative_path, content_type
                FROM artifacts
                JOIN jobs ON jobs.id = artifacts.job_id
                WHERE artifacts.id = ? AND artifacts.job_id = ? AND jobs.owner_id = ?
                """,
                (artifact_id, job_id, owner_id),
            ).fetchone()
        return None if row is None else StoredArtifact(**dict(row))

    def record_progress(self, job_id: str, completed: int, total: int) -> None:
        """Persist progress so a page refresh restores it without replaying socket events."""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET progress_completed = ?, progress_total = ?, updated_at = ?
                WHERE id = ?
                """,
                (completed, total, self._now(), job_id),
            )

    def replace_dataset_progress(self, job_id: str, rows: Sequence[DatasetStatus]) -> None:
        """The rows mirror what AISBench reports right now, so a scan replaces them wholly."""
        timestamp = self._now()
        with self.database.connect() as connection:
            connection.execute("DELETE FROM job_dataset_progress WHERE job_id = ?", (job_id,))
            connection.executemany(
                """
                INSERT INTO job_dataset_progress (
                  job_id, dataset, phase, raw_status, completed, total, rate, counters,
                  log_path, metrics_json, correct_count, total_count, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        job_id,
                        row.dataset,
                        row.phase,
                        row.raw_status,
                        row.completed,
                        row.total,
                        row.rate,
                        _json_or_none(row.counters),
                        row.log_path,
                        _json_or_none(row.metrics),
                        row.correct_count,
                        row.total_count,
                        row.started_at,
                        row.updated_at or timestamp,
                    )
                    for row in rows
                ],
            )

    def list_dataset_progress(self, job_id: str) -> list[StoredDatasetProgress]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT dataset, phase, raw_status, completed, total, rate, counters, log_path,
                       metrics_json, correct_count, total_count, started_at, updated_at
                FROM job_dataset_progress
                WHERE job_id = ?
                ORDER BY rowid
                """,
                (job_id,),
            ).fetchall()
        return [self._dataset_progress_from_row(row) for row in rows]

    def get_dataset_progress(self, job_id: str, dataset: str) -> StoredDatasetProgress | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT dataset, phase, raw_status, completed, total, rate, counters, log_path,
                       metrics_json, correct_count, total_count, started_at, updated_at
                FROM job_dataset_progress
                WHERE job_id = ? AND dataset = ?
                """,
                (job_id, dataset),
            ).fetchone()
        return None if row is None else self._dataset_progress_from_row(row)

    def store_dataset_results(
        self,
        job_id: str,
        per_dataset: dict[str, dict[str, tuple[float | None, str | None, str | None]]],
        counts: dict[str, tuple[int, int]],
    ) -> None:
        """Fill the result columns of the dataset rows of a run that succeeded.

        A merged run tracked one row under the merged task name while it ran; its results
        are per dataset, so the rows are rebuilt around what the results actually contain.
        A row that failed keeps its story: a dash row in the summary is the absence of a
        result, and overwriting it would hide the failure.
        """
        timestamp = self._now()
        with self.database.connect() as connection:
            existing = {
                row["dataset"]: row
                for row in connection.execute(
                    "SELECT dataset, phase, completed, total, log_path, metrics_json, "
                    "correct_count, total_count, started_at "
                    "FROM job_dataset_progress WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            }
            rows = []
            for name, row in existing.items():
                if row["phase"] != "failed":
                    continue
                rows.append(
                    (
                        job_id,
                        name,
                        row["phase"],
                        None,
                        row["completed"],
                        row["total"],
                        None,
                        None,
                        row["log_path"],
                        row["metrics_json"],
                        row["correct_count"],
                        row["total_count"],
                        row["started_at"],
                        timestamp,
                    )
                )
            for dataset, metrics in per_dataset.items():
                prior = existing.get(dataset)
                if prior is not None and prior["phase"] == "failed":
                    continue
                correct, total = counts.get(dataset, (None, None))
                rows.append(
                    (
                        job_id,
                        dataset,
                        "finished",
                        None,
                        prior["completed"] if prior is not None else None,
                        prior["total"] if prior is not None else None,
                        None,
                        None,
                        prior["log_path"] if prior is not None else None,
                        json.dumps(
                            {
                                name: {"value": value, "text_value": text, "unit": unit}
                                for name, (value, text, unit) in sorted(metrics.items())
                            },
                            ensure_ascii=False,
                        ),
                        correct,
                        total,
                        prior["started_at"] if prior is not None else None,
                        timestamp,
                    )
                )
            connection.execute("DELETE FROM job_dataset_progress WHERE job_id = ?", (job_id,))
            if rows:
                connection.executemany(
                    """
                    INSERT INTO job_dataset_progress (
                      job_id, dataset, phase, raw_status, completed, total, rate, counters,
                      log_path, metrics_json, correct_count, total_count, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    @staticmethod
    def _dataset_progress_from_row(row: sqlite3.Row) -> StoredDatasetProgress:
        return StoredDatasetProgress(
            dataset=row["dataset"],
            phase=row["phase"],
            raw_status=row["raw_status"],
            completed=row["completed"],
            total=row["total"],
            rate=row["rate"],
            counters=_none_or_json(row["counters"]),
            log_path=row["log_path"],
            metrics=_none_or_json(row["metrics_json"]),
            correct_count=row["correct_count"],
            total_count=row["total_count"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
        )

    def get_for_owner(self, job_id: str, owner_id: str) -> Job | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {SELECTED_COLUMNS} FROM jobs WHERE id = ? AND owner_id = ?",
                (job_id, owner_id),
            ).fetchone()
        return None if row is None else self._job_from_row(row)

    def list_for_owner(self, owner_id: str) -> list[Job]:
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {SELECTED_COLUMNS} FROM jobs
                WHERE owner_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (owner_id,),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def claim_next(self) -> Job | None:
        """Take the oldest queued job. BEGIN IMMEDIATE plus a status-guarded UPDATE means two
        workers can never be handed the same job."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.STARTING.value,
                    self._now(),
                    self._now(),
                    row["id"],
                    JobStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount == 0:
                return None
            claimed = connection.execute(
                f"SELECT {SELECTED_COLUMNS} FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return self._job_from_row(claimed)

    def queue_position(self, job_id: str) -> int | None:
        """Count queued jobs strictly ahead of this one without exposing any of their details."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, created_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None or row["status"] != JobStatus.QUEUED.value:
                return None
            return connection.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE status = ? AND (created_at, id) < (?, ?)
                """,
                (JobStatus.QUEUED.value, row["created_at"], job_id),
            ).fetchone()[0]

    def transition(self, job_id: str, target: JobStatus, **fields: Any) -> Job | None:
        return self._transition(job_id, target, owner_id=None, **fields)

    def request_stop_for_owner(self, job_id: str, owner_id: str) -> Job | None:
        """Queued work cancels outright; claimed work must be asked to stop first (spec 9)."""
        current = self.get_for_owner(job_id, owner_id)
        if current is None:
            return None
        if current.status == JobStatus.STOPPING.value:
            return current
        queued = current.status == JobStatus.QUEUED.value
        target = JobStatus.CANCELLED if queued else JobStatus.STOPPING
        return self._transition(job_id, target, owner_id=owner_id)

    def recover_interrupted(self) -> int:
        """Mark work this process can no longer be managing; queued work keeps its place."""
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        timestamp = self._now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET status = ?, finished_at = ?, updated_at = ?,
                    error_code = COALESCE(error_code, 'interrupted'),
                    error_message = COALESCE(
                      error_message, 'The service restarted while this job was running.'
                    )
                WHERE status IN ({placeholders})
                """,
                (
                    JobStatus.INTERRUPTED.value,
                    timestamp,
                    timestamp,
                    *[status.value for status in ACTIVE_STATUSES],
                ),
            )
        return cursor.rowcount

    def _transition(
        self,
        job_id: str,
        target: JobStatus,
        *,
        owner_id: str | None,
        **fields: Any,
    ) -> Job | None:
        assignments = [f"{column} = ?" for column in fields if column in UPDATABLE_COLUMNS]
        values = [value for column, value in fields.items() if column in UPDATABLE_COLUMNS]
        timestamp = self._now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?"
                + (" AND owner_id = ?" if owner_id else ""),
                (job_id, owner_id) if owner_id else (job_id,),
            ).fetchone()
            if row is None:
                return None
            require_transition(JobStatus(row["status"]), target)
            if target is JobStatus.RUNNING:
                assignments.append("started_at = COALESCE(started_at, ?)")
                values.append(timestamp)
            if target in TERMINAL_STATUSES:
                assignments.append("finished_at = ?")
                values.append(timestamp)
            connection.execute(
                f"""
                UPDATE jobs
                SET status = ?, {", ".join([*assignments, "updated_at = ?"])}
                WHERE id = ? AND status = ?
                """,
                (target.value, *values, timestamp, job_id, row["status"]),
            )
            updated = connection.execute(
                f"SELECT {SELECTED_COLUMNS} FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._job_from_row(updated)

    @staticmethod
    def _utc_now(now: datetime | None) -> datetime:
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return timestamp.astimezone(timezone.utc)

    @classmethod
    def _now(cls) -> str:
        return cls._utc_now(None).isoformat()

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            model_endpoint_id=row["model_endpoint_id"],
            dataset_id=row["dataset_id"],
            mode=row["mode"],
            status=row["status"],
            parameters=json.loads(row["parameters_json"]),
            model_snapshot=json.loads(row["model_snapshot_json"]),
            dataset_snapshot=json.loads(row["dataset_snapshot_json"]),
            config_path=row["config_path"],
            output_dir=row["output_dir"],
            log_path=row["log_path"],
            pid=row["pid"],
            exit_code=row["exit_code"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            progress_completed=row["progress_completed"],
            progress_total=row["progress_total"],
        )


def _json_or_none(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _none_or_json(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None

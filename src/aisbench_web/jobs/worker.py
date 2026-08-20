import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aisbench_web.db import Database
from aisbench_web.jobs.config_generator import (
    EndpointSnapshot,
    cli_arguments_for,
    cli_mode_for,
    generate_config,
    render_config,
)
from aisbench_web.jobs.dataset_progress import DatasetProgressCollector
from aisbench_web.jobs.notifier import JobNotifier
from aisbench_web.jobs.process_runner import ProcessRunner
from aisbench_web.jobs.progress import parse_progress
from aisbench_web.jobs.results import ParsedResults, index_artifacts, parse_results
from aisbench_web.jobs.states import JobStatus
from aisbench_web.repositories.jobs import Job, JobRepository, dataset_entries
from aisbench_web.security import api_key_cipher, load_or_create_secret
from aisbench_web.settings import Settings

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.1
IDLE_INTERVAL_SECONDS = 0.5
#: A custom parser returns results, or None when it could not tell.
ResultParser = Callable[[Job, Path], ParsedResults | None]


def recover_interrupted_jobs(repository: JobRepository) -> int:
    """Mark work no process can still be managing; queued jobs keep their place."""
    interrupted = repository.recover_interrupted()
    if interrupted:
        logger.warning("Marked %s job(s) interrupted after an unclean shutdown", interrupted)
    return interrupted


class Worker:
    """Single-process FIFO worker: claim a job, run AISBench, record how it ended."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        runner: ProcessRunner | None = None,
        result_parser: ResultParser | None = None,
        notifier: JobNotifier | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.database = database
        self.settings = settings
        self.repository = JobRepository(database)
        self.runner = runner or ProcessRunner()
        self.result_parser = result_parser or self._store_results
        self.notifier = notifier
        self.poll_interval = poll_interval
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._managed: dict[str, object] = {}
        # Reserved at claim time, released when the job ends: capacity must account for a job
        # between being claimed and having a process, or the loop over-claims.
        self._inflight: set[str] = set()
        self._managed_guard = threading.Lock()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="aisbench-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Stop claiming, then interrupt only the process groups this worker still owns."""
        self._stopping.set()
        for job_id, process in self._take_managed():
            self.runner.terminate(process, expected_pid=self._recorded_pid(job_id))
            self._finish(
                job_id,
                JobStatus.INTERRUPTED,
                exit_code=None,
                error_code="shutdown",
                error_message="The service shut down while this job was running.",
            )
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        with ThreadPoolExecutor(
            max_workers=self.settings.max_concurrent_jobs,
            thread_name_prefix="aisbench-job",
        ) as pool:
            while not self._stopping.is_set():
                try:
                    if self._at_capacity():
                        time.sleep(self.poll_interval)
                        continue
                    job = self.repository.claim_next()
                    if job is None:
                        time.sleep(IDLE_INTERVAL_SECONDS)
                        continue
                    self._reserve(job.id)
                    pool.submit(self._run_reserved, job)
                except Exception:
                    logger.exception("Worker loop failed; continuing")
                    time.sleep(IDLE_INTERVAL_SECONDS)

    # -- execution ------------------------------------------------------------

    def run_pending_once(self) -> bool:
        """Claim and run the oldest queued job to completion, inline.

        Returns False when the queue is empty or the worker is already full.
        """
        if self._stopping.is_set() or self._at_capacity():
            return False
        job = self.repository.claim_next()
        if job is None:
            return False
        self._reserve(job.id)
        self._run_reserved(job)
        return True

    def _run_reserved(self, job: Job) -> None:
        try:
            self._run(job)
        finally:
            self._release(job.id)

    def _run(self, job: Job) -> None:
        job_dir = self.settings.jobs_dir / job.id
        config_path = self.settings.jobs_dir / job.config_path
        output_dir = self.settings.jobs_dir / job.output_dir
        log_path = self.settings.jobs_dir / job.log_path

        try:
            endpoint = self._endpoint_snapshot(job)
            datasets = self._dataset_config(job)
            job_dir.mkdir(parents=True, exist_ok=True)
            generate_config(
                config_path,
                mode=job.mode,
                datasets=datasets,
                endpoint=endpoint,
                parameters=job.parameters,
                model_import=job.model_snapshot.get("config_import") or None,
            )
            process = self.runner.launch(
                ais_bench_path=self.settings.ais_bench_path,
                config_path=config_path,
                cli_mode=cli_mode_for(job.mode, job.parameters),
                output_dir=output_dir,
                log_path=log_path,
                job_dir=job_dir,
                extra_arguments=cli_arguments_for(job.parameters),
            )
        except Exception as exc:
            logger.warning("Could not start job %s", job.id, exc_info=exc)
            self._finish(
                job.id,
                JobStatus.FAILED,
                exit_code=None,
                error_code="launch_failed",
                error_message=str(exc) or exc.__class__.__name__,
            )
            self._redact_config(job, config_path)
            return

        with self._managed_guard:
            self._managed[job.id] = process
        try:
            # PID before RUNNING: a crash between the two must not leave a running job whose
            # process nobody can find.
            self.repository.transition(job.id, JobStatus.RUNNING, pid=process.pid)
            self._publish(job.id, {"type": "status", "status": JobStatus.RUNNING.value})
            self._await_exit(job, process)
        finally:
            with self._managed_guard:
                self._managed.pop(job.id, None)
            self._redact_config(job, config_path)

    def _await_exit(self, job: Job, process) -> None:
        stop_requested = False
        tail = _LogTail(self.settings.jobs_dir / job.log_path)
        collector = DatasetProgressCollector(
            model_abbr=str(job.model_snapshot.get("abbr") or ""),
            output_dir=self.settings.jobs_dir / job.output_dir,
        )
        self._seed_dataset_rows(job, collector)
        collector.start()
        # The rows exist before AISBench names its tasks; a page opened this early must
        # not stare at a blank column while the interpreter is still starting.
        self._store_dataset_progress(job.id, collector)
        while process.poll() is None:
            if self._stopping.is_set():
                return
            if not stop_requested and self._stop_requested(job.id):
                stop_requested = True
                self.runner.terminate(process, expected_pid=self._recorded_pid(job.id))
            lines = tail.new_lines()
            self._publish_progress(job.id, lines)
            collector.consume_lines(lines)
            collector.scan()
            if collector.dirty:
                collector.dirty = False
                self._store_dataset_progress(job.id, collector)
            time.sleep(self.poll_interval)

        lines = tail.new_lines()
        self._publish_progress(job.id, lines)
        collector.consume_lines(lines)
        exit_code = process.returncode
        cancelled = stop_requested or self._stop_requested(job.id)
        collector.finish(succeeded=exit_code == 0 and not cancelled)
        self._store_dataset_progress(job.id, collector)

        if self._stopping.is_set():
            # The child may exit while stop() is terminating it; stop() records the outcome.
            return

        if cancelled:
            self._finish(job.id, JobStatus.CANCELLED, exit_code=exit_code)
            return
        if exit_code == 0:
            parsed = self._parse_results(job)
            # AISBench finishes its workflow with exit 0 even when every task failed, so
            # the results are what decides: none at all means the run failed, not passed.
            if parsed is not None and not parsed.metrics:
                self._finish(
                    job.id,
                    JobStatus.FAILED,
                    exit_code=exit_code,
                    error_code="no_results",
                    error_message=(
                        "AISBench exited successfully but produced no results; "
                        "its tasks failed — see the log."
                    ),
                )
                return
            self._finish(job.id, JobStatus.SUCCEEDED, exit_code=exit_code)
            return
        self._finish(
            job.id,
            JobStatus.FAILED,
            exit_code=exit_code,
            error_code="nonzero_exit",
            error_message=f"AISBench exited with status {exit_code}",
        )

    @staticmethod
    def _seed_dataset_rows(job: Job, collector: DatasetProgressCollector) -> None:
        """Every chosen dataset gets a row from the start, before AISBench names its tasks."""
        for entry in dataset_entries(job.dataset_snapshot):
            collector.seed(str(entry.get("abbr") or ""))

    def _store_dataset_progress(self, job_id: str, collector: DatasetProgressCollector) -> None:
        """Persist before publishing: a page refreshed a moment later must see the same rows."""
        try:
            self.repository.replace_dataset_progress(job_id, list(collector.states.values()))
        except Exception:
            logger.exception("Storing dataset progress for job %s failed", job_id)
            return
        self._publish(job_id, {"type": "datasets"})

    def _publish_progress(self, job_id: str, lines: list[str]) -> None:
        """Report only progress the log actually states; unreadable output stays unreported."""
        latest = None
        for line in lines:
            parsed = parse_progress(line)
            if parsed is not None:
                latest = parsed
        if latest is None:
            return
        completed, total = latest
        # Persist before publishing: a page refreshed a moment later must see the same number.
        self.repository.record_progress(job_id, completed, total)
        self._publish(job_id, {"type": "progress", "completed": completed, "total": total})

    def _store_results(self, job: Job, output_dir: Path) -> ParsedResults:
        parsed = parse_results(job.mode, output_dir)
        for warning in parsed.warnings:
            logger.info("Job %s: %s", job.id, warning)
        self.repository.replace_metrics(
            job.id,
            {
                key: (metric.value, metric.text_value, metric.unit)
                for key, metric in parsed.metrics.items()
            },
        )
        self.repository.replace_artifacts(
            job.id,
            [
                (artifact.kind, artifact.relative_path, artifact.content_type)
                for artifact in index_artifacts(output_dir)
            ],
        )
        # The per-dataset rows carry what each dataset scored, next to how far it got.
        self.repository.store_dataset_results(
            job.id,
            {
                dataset: {
                    metric.key: (metric.value, metric.text_value, metric.unit)
                    for metric in metrics.values()
                }
                for dataset, metrics in parsed.per_dataset.items()
            },
            parsed.counts,
        )
        return parsed

    def _parse_results(self, job: Job) -> ParsedResults | None:
        try:
            return self.result_parser(job, self.settings.jobs_dir / job.output_dir)
        except Exception:
            logger.exception("Parsing results for job %s failed", job.id)
            return None

    # -- helpers --------------------------------------------------------------

    def _endpoint_snapshot(self, job: Job) -> EndpointSnapshot:
        snapshot = job.model_snapshot
        encrypted = snapshot.get("encrypted_api_key")
        api_key = None
        if encrypted:
            cipher = api_key_cipher(load_or_create_secret(self.settings.secret_path))
            api_key = cipher.decrypt(encrypted.encode("utf-8")).decode("utf-8")
        return EndpointSnapshot(
            abbr=snapshot.get("abbr") or f"job-{job.id[:8]}",
            base_url=snapshot["base_url"],
            model_name=snapshot["model_name"],
            api_key=api_key,
        )

    @staticmethod
    def _dataset_config(job: Job) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for entry in dataset_entries(job.dataset_snapshot):
            config_import = entry.get("config_import")
            symbol = entry.get("dataset_symbol")
            if not config_import or not symbol:
                raise ValueError(f"job {job.id} has no dataset config for mode {job.mode!r}")
            pairs.append((config_import, symbol))
        if not pairs:
            raise ValueError(f"job {job.id} has no dataset config for mode {job.mode!r}")
        return pairs

    def _redact_config(self, job: Job, config_path: Path) -> None:
        """Replace the on-disk config so a decrypted API key is not kept at rest."""
        if not config_path.exists():
            return
        try:
            datasets = self._dataset_config(job)
            redacted = render_config(
                mode=job.mode,
                datasets=datasets,
                endpoint=self._endpoint_snapshot(job),
                parameters=job.parameters,
                model_import=job.model_snapshot.get("config_import") or None,
                redact_api_key=True,
            )
        except Exception:
            logger.exception("Could not render a redacted config for job %s", job.id)
            config_path.unlink(missing_ok=True)
            return
        config_path.write_text(redacted, encoding="utf-8")
        config_path.chmod(0o600)

    def _stop_requested(self, job_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row is not None and row["status"] == JobStatus.STOPPING.value

    def _recorded_pid(self, job_id: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT pid FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else row["pid"]

    def _finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        exit_code: int | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            self.repository.transition(
                job_id,
                status,
                exit_code=exit_code,
                error_code=error_code,
                error_message=error_message,
            )
        except ValueError:
            logger.warning("Job %s already reached a terminal state", job_id)
            return
        self._publish(
            job_id,
            {
                "type": "status",
                "status": status.value,
                "exit_code": exit_code,
                "error_code": error_code,
            },
        )

    def _publish(self, job_id: str, event: dict) -> None:
        if self.notifier is not None:
            self.notifier.publish_threadsafe(job_id, event)

    def _at_capacity(self) -> bool:
        with self._managed_guard:
            return len(self._inflight) >= self.settings.max_concurrent_jobs

    def _reserve(self, job_id: str) -> None:
        with self._managed_guard:
            self._inflight.add(job_id)

    def _release(self, job_id: str) -> None:
        with self._managed_guard:
            self._inflight.discard(job_id)

    def _take_managed(self) -> list[tuple[str, object]]:
        with self._managed_guard:
            taken = list(self._managed.items())
            self._managed.clear()
        return taken


class _LogTail:
    """Read whole lines appended to the process log since the last poll."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = ""

    def new_lines(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []
        if not chunk:
            return []
        text = self._partial + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._partial = lines.pop()
        return lines

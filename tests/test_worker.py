import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from aisbench_web.db import Database
from aisbench_web.jobs.notifier import JobNotifier
from aisbench_web.jobs.process_runner import ProcessRunner, sanitized_environment
from aisbench_web.jobs.progress import parse_progress
from aisbench_web.jobs.states import JobStatus
from aisbench_web.jobs.worker import Worker, recover_interrupted_jobs
from aisbench_web.repositories.jobs import JobRepository
from aisbench_web.security import api_key_cipher, load_or_create_secret
from aisbench_web.settings import Settings

FAKE_AIS_BENCH = Path(__file__).parent / "fake_ais_bench.py"
GSM8K_IMPORT = "ais_bench.benchmark.configs.datasets.gsm8k.gsm8k_gen_4_shot_cot_chat_prompt"
API_KEY = "secret-model-token"


@dataclass
class Harness:
    worker: Worker
    jobs: JobRepository
    settings: Settings
    wrapper: Path
    database: Database
    owner: str = "user-alice"

    def set_scenario(self, scenario: str) -> None:
        """Rewrite the stand-in executable. The worker's sanitized environment intentionally
        drops unknown variables, so the scenario travels on the command line instead."""
        self.wrapper.write_text(
            f'#!/bin/sh\nexec {sys.executable} {FAKE_AIS_BENCH} "$@" --scenario {scenario}\n',
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)

    def queue(self, *, scenario: str = "success", mode: str = "accuracy", owner: str | None = None):
        return self.jobs.create(
            owner_id=owner or self.owner,
            model_endpoint_id="endpoint-1",
            dataset_id="gsm8k",
            mode=mode,
            parameters={"num_prompts": 8, "scenario": scenario},
            model_snapshot={
                "abbr": "job-model",
                "base_url": "http://127.0.0.1:8001/v1",
                "model_name": "Qwen3-32B",
                "encrypted_api_key": self.encrypted_api_key,
                "max_output_length": 512,
            },
            dataset_snapshot={
                "id": "gsm8k",
                "config_import": GSM8K_IMPORT,
                "dataset_symbol": "gsm8k_datasets",
            },
        )

    @property
    def encrypted_api_key(self) -> str:
        cipher = api_key_cipher(load_or_create_secret(self.settings.secret_path))
        return cipher.encrypt(API_KEY.encode()).decode()

    def log_of(self, job) -> str:
        path = self.settings.jobs_dir / job.log_path
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def config_of(self, job) -> str:
        path = self.settings.jobs_dir / job.config_path
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def wait_for(self, job_id: str, statuses, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        wanted = {status.value for status in statuses}
        while time.monotonic() < deadline:
            current = self.jobs.get_for_owner(job_id, self.owner)
            if current is not None and current.status in wanted:
                return current
            time.sleep(0.02)
        raise AssertionError(
            f"job {job_id} stayed {self.jobs.get_for_owner(job_id, self.owner).status}"
        )

    def seed_running(self, pid: int):
        job = self.queue()
        self.jobs.claim_next()
        self.jobs.transition(job.id, JobStatus.RUNNING, pid=pid)
        return job


@pytest.fixture
def harness(tmp_path: Path, database: Database, settings: Settings) -> Harness:
    database.migrate()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO users (id, username, username_key, password_hash, created_at)
            VALUES ('user-alice', 'alice', 'alice', 'hash', 'created')
            """
        )
        connection.execute(
            """
            INSERT INTO model_endpoints (
              id, owner_id, name, base_url, model_name, created_at, updated_at
            ) VALUES (
              'endpoint-1', 'user-alice', 'qwen', 'http://127.0.0.1:8001/v1', 'Qwen3-32B',
              'c', 'u'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO datasets (id, config_name, name, description, status, updated_at)
            VALUES ('gsm8k', 'gsm8k_gen', 'GSM8K', 'math', 'available', 'u')
            """
        )
    wrapper = tmp_path / "ais_bench"
    runnable = Settings.create(settings.data_dir, wrapper, settings.max_concurrent_jobs)
    worker = Worker(database, runnable, poll_interval=0.02)
    harness = Harness(
        worker=worker,
        jobs=JobRepository(database),
        settings=runnable,
        wrapper=wrapper,
        database=database,
    )
    harness.set_scenario("success")
    yield harness
    worker.stop(timeout=5)


# --- progress ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("PROGRESS 8/8", (8, 8)),
        ("  PROGRESS 2 / 8  ", (2, 8)),
        (" 50%|#####     | 4/8 [00:01<00:01,  2.0it/s]", (4, 8)),
        ("loading dataset gsm8k", None),
        ("PROGRESS 9/8", None),
        ("PROGRESS 1/0", None),
        ("accuracy 87.50", None),
        ("", None),
    ],
)
def test_progress_is_read_or_reported_as_unknown(line: str, expected) -> None:
    assert parse_progress(line) == expected


# --- environment -------------------------------------------------------------


def test_child_environment_keeps_the_toolchain_and_drops_service_settings() -> None:
    environment = sanitized_environment(
        {
            "PATH": "/usr/bin",
            "CONDA_PREFIX": "/opt/conda/envs/ais_bench",
            "LD_LIBRARY_PATH": "/opt/ascend/lib",
            "AISBENCH_DATASETS_DIR": "/data/sets",
            "AISBENCH_WEB_AIS_BENCH_PATH": "/private/path",
            "AWS_SECRET_ACCESS_KEY": "leak",
            "RANDOM_UNRELATED": "x",
        }
    )

    assert environment == {
        "PATH": "/usr/bin",
        "CONDA_PREFIX": "/opt/conda/envs/ais_bench",
        "LD_LIBRARY_PATH": "/opt/ascend/lib",
        "AISBENCH_DATASETS_DIR": "/data/sets",
    }


def test_command_line_matches_the_documented_cli() -> None:
    command = ProcessRunner().build_command(
        ais_bench_path=Path("/usr/bin/ais_bench"),
        config_path=Path("/jobs/j1/generated_config.py"),
        cli_mode="perf_viz",
        output_dir=Path("/jobs/j1/outputs"),
    )

    assert command == [
        "/usr/bin/ais_bench",
        "/jobs/j1/generated_config.py",
        "--mode",
        "perf_viz",
        "--work-dir",
        "/jobs/j1/outputs",
    ]


# --- execution ---------------------------------------------------------------


def test_worker_runs_oldest_job_and_captures_logs(harness: Harness) -> None:
    first = harness.queue()
    second = harness.queue()

    assert harness.worker.run_pending_once() is True

    assert harness.jobs.get_for_owner(first.id, harness.owner).status == JobStatus.SUCCEEDED
    assert "PROGRESS 8/8" in harness.log_of(first)
    assert harness.jobs.get_for_owner(second.id, harness.owner).status == JobStatus.QUEUED
    summary = harness.settings.jobs_dir / first.output_dir / "summary" / "summary_test.csv"
    assert summary.exists()


def test_exit_code_zero_is_success_and_nonzero_is_failure(harness: Harness) -> None:
    harness.set_scenario("fail")
    job = harness.queue()

    harness.worker.run_pending_once()

    finished = harness.jobs.get_for_owner(job.id, harness.owner)
    assert finished.status == JobStatus.FAILED
    assert finished.exit_code == 3
    assert finished.error_code == "nonzero_exit"
    assert "refused the request" in harness.log_of(job)


def test_a_job_that_cannot_start_fails_without_a_process(harness: Harness) -> None:
    job = harness.jobs.create(
        owner_id=harness.owner,
        model_endpoint_id="endpoint-1",
        dataset_id="gsm8k",
        mode="accuracy",
        parameters={},
        model_snapshot={"base_url": "http://127.0.0.1:8001/v1", "model_name": "m"},
        dataset_snapshot={"id": "gsm8k"},
    )

    harness.worker.run_pending_once()

    finished = harness.jobs.get_for_owner(job.id, harness.owner)
    assert finished.status == JobStatus.FAILED
    assert finished.error_code == "launch_failed"
    assert finished.pid is None


def test_running_job_records_its_pid_before_running_state(harness: Harness) -> None:
    job = harness.queue()

    harness.worker.run_pending_once()

    assert harness.jobs.get_for_owner(job.id, harness.owner).pid is not None


# --- secrets at rest ---------------------------------------------------------


def test_the_decrypted_key_is_removed_from_the_config_after_the_run(harness: Harness) -> None:
    job = harness.queue()

    harness.worker.run_pending_once()

    config = harness.config_of(job)
    assert API_KEY not in config
    assert "api_key='***'" in config
    config_path = harness.settings.jobs_dir / job.config_path
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_the_key_is_redacted_even_when_the_run_fails(harness: Harness) -> None:
    harness.set_scenario("fail")
    job = harness.queue()

    harness.worker.run_pending_once()

    assert API_KEY not in harness.config_of(job)


# --- cancellation ------------------------------------------------------------


def test_cancel_terminates_the_process_group(harness: Harness) -> None:
    harness.set_scenario("sleep")
    job = harness.queue()
    harness.worker.start()
    running = harness.wait_for(job.id, [JobStatus.RUNNING])
    pid = running.pid

    harness.jobs.request_stop_for_owner(job.id, harness.owner)
    cancelled = harness.wait_for(job.id, [JobStatus.CANCELLED])

    assert cancelled.status == JobStatus.CANCELLED
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.02)
    assert not _pid_alive(pid), "the process group outlived cancellation"


def test_terminate_refuses_a_pid_the_job_no_longer_owns(tmp_path: Path) -> None:
    # start_new_session gives the child its own process group. Without it a regression in the
    # pid guard would killpg the test runner itself instead of failing this assertion.
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        result = ProcessRunner().terminate(process, expected_pid=process.pid + 100000)
        assert result is None
        assert process.poll() is None, "an unrelated pid must not be signalled"
    finally:
        process.kill()
        process.wait(timeout=5)


# --- recovery ----------------------------------------------------------------


def test_startup_marks_orphaned_running_jobs_interrupted(harness: Harness) -> None:
    orphan = harness.seed_running(pid=999999)

    assert recover_interrupted_jobs(harness.jobs) == 1

    assert harness.jobs.get_for_owner(orphan.id, harness.owner).status == JobStatus.INTERRUPTED


def test_recovery_leaves_queued_jobs_queued(harness: Harness) -> None:
    queued = harness.queue()

    recover_interrupted_jobs(harness.jobs)

    assert harness.jobs.get_for_owner(queued.id, harness.owner).status == JobStatus.QUEUED


def test_shutdown_interrupts_only_processes_this_worker_manages(harness: Harness) -> None:
    unrelated = harness.seed_running(pid=999999)
    harness.set_scenario("sleep")
    managed = harness.queue()
    harness.worker.start()
    harness.wait_for(managed.id, [JobStatus.RUNNING])

    harness.worker.stop(timeout=15)

    assert harness.jobs.get_for_owner(managed.id, harness.owner).status == JobStatus.INTERRUPTED
    assert harness.jobs.get_for_owner(unrelated.id, harness.owner).status == JobStatus.RUNNING


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# --- results ------------------------------------------------------------------


def test_a_successful_run_stores_metrics_and_artifacts(harness: Harness) -> None:
    job = harness.queue()

    harness.worker.run_pending_once()

    metrics = {m.key: m for m in harness.jobs.list_metrics_for_owner(job.id, harness.owner)}
    artifacts = {a.relative_path: a for a in harness.jobs.list_artifacts_for_owner(job.id, harness.owner)}
    assert metrics["gsm8k.accuracy"].value == 87.5
    assert artifacts["summary/summary_test.csv"].kind == "summary"
    assert artifacts["summary/summary_test.csv"].content_type == "text/csv"


def test_a_failed_run_stores_no_metrics(harness: Harness) -> None:
    harness.set_scenario("fail")
    job = harness.queue()

    harness.worker.run_pending_once()

    assert harness.jobs.list_metrics_for_owner(job.id, harness.owner) == []


# --- lifespan wiring ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_app_lifespan_starts_and_stops_the_worker(harness: Harness) -> None:
    """The worker must be reachable through a real app start, not only when driven directly."""
    from aisbench_web.app import create_app

    job = harness.queue()
    app = create_app(settings=harness.settings, start_worker=True)

    async with app.router.lifespan_context(app):
        assert app.state.worker is not None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if harness.jobs.get_for_owner(job.id, harness.owner).status == JobStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.02)

    assert harness.jobs.get_for_owner(job.id, harness.owner).status == JobStatus.SUCCEEDED
    assert app.state.worker._thread is None


@pytest.mark.asyncio
async def test_a_stopped_app_interrupts_the_job_it_was_running(harness: Harness) -> None:
    from aisbench_web.app import create_app

    harness.set_scenario("sleep")
    job = harness.queue()
    app = create_app(settings=harness.settings, start_worker=True)

    async with app.router.lifespan_context(app):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if harness.jobs.get_for_owner(job.id, harness.owner).status == JobStatus.RUNNING:
                break
            await asyncio.sleep(0.02)
        assert harness.jobs.get_for_owner(job.id, harness.owner).status == JobStatus.RUNNING

    assert harness.jobs.get_for_owner(job.id, harness.owner).status == JobStatus.INTERRUPTED


# --- live notifications -------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_publishes_progress_and_final_status(harness: Harness) -> None:
    """The worker runs in a thread; events must cross into the serving loop's queues."""
    notifier = JobNotifier()
    notifier.bind_loop(asyncio.get_running_loop())
    worker = Worker(harness.database, harness.settings, notifier=notifier, poll_interval=0.02)
    job = harness.queue()
    queue = notifier.subscribe(job.id)

    await asyncio.get_running_loop().run_in_executor(None, worker.run_pending_once)
    await asyncio.sleep(0.05)  # let call_soon_threadsafe callbacks drain

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert {"type": "status", "status": "running"} in events
    assert any(
        event["type"] == "progress" and (event["completed"], event["total"]) == (8, 8)
        for event in events
    )
    assert any(
        event["type"] == "status" and event["status"] == "succeeded" for event in events
    )


@pytest.mark.asyncio
async def test_no_progress_is_invented_when_the_log_says_nothing(harness: Harness) -> None:
    harness.set_scenario("fail")
    notifier = JobNotifier()
    notifier.bind_loop(asyncio.get_running_loop())
    worker = Worker(harness.database, harness.settings, notifier=notifier, poll_interval=0.02)
    job = harness.queue()
    queue = notifier.subscribe(job.id)

    await asyncio.get_running_loop().run_in_executor(None, worker.run_pending_once)
    await asyncio.sleep(0.05)

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert not [event for event in events if event["type"] == "progress"]
    assert any(event.get("status") == "failed" for event in events)

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aisbench_web.db import Database
from aisbench_web.jobs.states import ALLOWED_TRANSITIONS, JobStatus, require_transition
from aisbench_web.repositories.jobs import JobRepository

CIPHERTEXT = "gAAAAABm-not-a-real-token"


@dataclass(frozen=True)
class Fixtures:
    alice: str
    bob: str
    endpoint: str
    dataset: str


@pytest.fixture
def seeded(database: Database) -> Fixtures:
    database.migrate()
    with database.connect() as connection:
        for user_id, name in (("user-alice", "alice"), ("user-bob", "bob")):
            connection.execute(
                """
                INSERT INTO users (id, username, username_key, password_hash, created_at)
                VALUES (?, ?, ?, 'hash', '2026-08-17T00:00:00+00:00')
                """,
                (user_id, name, name),
            )
        connection.execute(
            """
            INSERT INTO model_endpoints (
              id, owner_id, name, base_url, model_name, encrypted_api_key,
              created_at, updated_at
            ) VALUES (
              'endpoint-1', 'user-alice', 'qwen', 'http://127.0.0.1:8001/v1', 'Qwen3-32B', ?,
              'created', 'updated'
            )
            """,
            (CIPHERTEXT.encode(),),
        )
        connection.execute(
            """
            INSERT INTO datasets (id, config_name, name, description, status, updated_at)
            VALUES ('gsm8k', 'gsm8k_gen_4_shot_cot_chat_prompt', 'GSM8K', 'math', 'available', 'u')
            """
        )
    return Fixtures(alice="user-alice", bob="user-bob", endpoint="endpoint-1", dataset="gsm8k")


@pytest.fixture
def job_repository(database: Database, seeded: Fixtures) -> JobRepository:
    return JobRepository(database)


def create_job(repository: JobRepository, seeded: Fixtures, *, owner: str, created_at=None):
    return repository.create(
        owner_id=owner,
        model_endpoint_id=seeded.endpoint,
        dataset_id=seeded.dataset,
        mode="accuracy",
        parameters={"num_prompts": 8},
        model_snapshot={"id": seeded.endpoint, "encrypted_api_key": CIPHERTEXT},
        dataset_snapshot={"id": seeded.dataset, "relative_data_path": "gsm8k"},
        now=created_at,
    )


# --- state machine -----------------------------------------------------------


def test_state_machine_rejects_terminal_transition() -> None:
    with pytest.raises(ValueError, match="illegal job transition"):
        require_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)


def test_every_terminal_state_is_final() -> None:
    for terminal in (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    ):
        assert ALLOWED_TRANSITIONS[terminal] == set()
        for target in JobStatus:
            with pytest.raises(ValueError, match="illegal job transition"):
                require_transition(terminal, target)


def test_state_machine_matches_the_documented_lifecycle() -> None:
    require_transition(JobStatus.QUEUED, JobStatus.STARTING)
    require_transition(JobStatus.QUEUED, JobStatus.CANCELLED)
    require_transition(JobStatus.STARTING, JobStatus.RUNNING)
    require_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)
    require_transition(JobStatus.RUNNING, JobStatus.STOPPING)
    require_transition(JobStatus.STOPPING, JobStatus.CANCELLED)
    require_transition(JobStatus.RUNNING, JobStatus.INTERRUPTED)

    with pytest.raises(ValueError, match="illegal job transition"):
        require_transition(JobStatus.QUEUED, JobStatus.RUNNING)
    with pytest.raises(ValueError, match="illegal job transition"):
        require_transition(JobStatus.STOPPING, JobStatus.SUCCEEDED)


# --- ownership ---------------------------------------------------------------


def test_job_queries_are_owner_scoped(job_repository: JobRepository, seeded: Fixtures) -> None:
    job = create_job(job_repository, seeded, owner=seeded.alice)

    assert job_repository.get_for_owner(job.id, seeded.alice).id == job.id
    assert job_repository.get_for_owner(job.id, seeded.bob) is None
    assert job_repository.list_for_owner(seeded.bob) == []
    assert [owned.id for owned in job_repository.list_for_owner(seeded.alice)] == [job.id]


def test_cancellation_is_owner_scoped(job_repository: JobRepository, seeded: Fixtures) -> None:
    job = create_job(job_repository, seeded, owner=seeded.alice)

    assert job_repository.request_stop_for_owner(job.id, seeded.bob) is None
    assert job_repository.get_for_owner(job.id, seeded.alice).status == JobStatus.QUEUED

    stopped = job_repository.request_stop_for_owner(job.id, seeded.alice)
    assert stopped.status == JobStatus.CANCELLED


def test_stopping_a_running_job_asks_before_it_cancels(
    job_repository: JobRepository,
    seeded: Fixtures,
) -> None:
    job = create_job(job_repository, seeded, owner=seeded.alice)
    job_repository.claim_next()
    job_repository.transition(job.id, JobStatus.RUNNING, pid=4242)

    stopped = job_repository.request_stop_for_owner(job.id, seeded.alice)

    assert stopped.status == JobStatus.STOPPING
    assert job_repository.request_stop_for_owner(job.id, seeded.alice).status == JobStatus.STOPPING


# --- snapshots ---------------------------------------------------------------


def test_snapshots_are_immutable_and_never_hold_plaintext_keys(
    job_repository: JobRepository,
    seeded: Fixtures,
    database: Database,
) -> None:
    job = create_job(job_repository, seeded, owner=seeded.alice)

    with database.connect() as connection:
        connection.execute(
            "UPDATE model_endpoints SET base_url = 'http://moved', name = 'renamed'"
        )
        stored = connection.execute(
            "SELECT model_snapshot_json, dataset_snapshot_json, parameters_json FROM jobs"
        ).fetchone()

    reread = job_repository.get_for_owner(job.id, seeded.alice)
    assert reread.model_snapshot == job.model_snapshot
    assert reread.parameters == {"num_prompts": 8}
    assert json.loads(stored["model_snapshot_json"])["encrypted_api_key"] == CIPHERTEXT
    assert "moved" not in stored["model_snapshot_json"]


def test_job_paths_stay_relative_to_the_jobs_directory(
    job_repository: JobRepository,
    seeded: Fixtures,
) -> None:
    job = create_job(job_repository, seeded, owner=seeded.alice)

    for stored_path in (job.config_path, job.output_dir, job.log_path):
        assert not Path(stored_path).is_absolute()
        assert ".." not in Path(stored_path).parts
        assert Path(stored_path).parts[0] == job.id


# --- FIFO queue --------------------------------------------------------------


def test_fifo_claim_uses_created_time_then_id(
    job_repository: JobRepository,
    seeded: Fixtures,
) -> None:
    base = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    first = create_job(job_repository, seeded, owner=seeded.alice, created_at=base)
    second = create_job(job_repository, seeded, owner=seeded.bob, created_at=base)
    third = create_job(
        job_repository, seeded, owner=seeded.alice, created_at=base + timedelta(seconds=1)
    )
    expected = sorted([first.id, second.id])

    claimed = [job_repository.claim_next(), job_repository.claim_next()]

    assert [job.id for job in claimed] == expected
    assert all(job.status == JobStatus.STARTING for job in claimed)
    assert job_repository.claim_next().id == third.id
    assert job_repository.claim_next() is None


def test_interleaved_claims_cannot_hand_out_the_same_job(
    job_repository: JobRepository,
    seeded: Fixtures,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force both claimers past the queued SELECT before either writes.

    A plain thread race does not reproduce this: whichever protection runs first hides the
    other. Pausing inside the claim transaction makes the interleaving deterministic, so the
    test fails if the transaction and the status guard are both gone.
    """
    create_job(job_repository, seeded, owner=seeded.alice)
    both_selected = threading.Barrier(2)
    real_connect = sqlite3.connect

    class InterleavingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = super().execute(sql, parameters)
            if "SELECT id FROM jobs" in sql:
                try:
                    # Times out when the write transaction correctly serialized the claimers.
                    both_selected.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
            return result

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: real_connect(*args, **kwargs, factory=InterleavingConnection),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(job_repository.claim_next) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]

    assert len([job for job in results if job is not None]) == 1


def test_queue_position_counts_jobs_ahead_without_revealing_them(
    job_repository: JobRepository,
    seeded: Fixtures,
) -> None:
    base = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    ahead = create_job(job_repository, seeded, owner=seeded.bob, created_at=base)
    mine = create_job(
        job_repository, seeded, owner=seeded.alice, created_at=base + timedelta(seconds=1)
    )

    assert job_repository.queue_position(ahead.id) == 0
    assert job_repository.queue_position(mine.id) == 1

    job_repository.claim_next()

    assert job_repository.queue_position(mine.id) == 0
    assert job_repository.queue_position("missing") is None


# --- recovery ----------------------------------------------------------------


def test_restart_interrupts_orphans_and_keeps_the_queue(
    job_repository: JobRepository,
    seeded: Fixtures,
) -> None:
    starting = create_job(job_repository, seeded, owner=seeded.alice)
    running = create_job(job_repository, seeded, owner=seeded.alice)
    job_repository.claim_next()
    job_repository.claim_next()
    job_repository.transition(running.id, JobStatus.RUNNING, pid=99)

    interrupted = job_repository.recover_interrupted()

    assert interrupted == 2
    statuses = {
        job.id: job.status for job in job_repository.list_for_owner(seeded.alice)
    }
    assert statuses[starting.id] == JobStatus.INTERRUPTED
    assert statuses[running.id] == JobStatus.INTERRUPTED

    still_queued = create_job(job_repository, seeded, owner=seeded.alice)
    assert job_repository.recover_interrupted() == 0
    assert job_repository.get_for_owner(still_queued.id, seeded.alice).status == JobStatus.QUEUED


def test_transitions_through_the_repository_are_validated(
    job_repository: JobRepository,
    seeded: Fixtures,
) -> None:
    job = create_job(job_repository, seeded, owner=seeded.alice)

    with pytest.raises(ValueError, match="illegal job transition"):
        job_repository.transition(job.id, JobStatus.RUNNING)

    assert job_repository.get_for_owner(job.id, seeded.alice).status == JobStatus.QUEUED

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.testclient import TestClient

from aisbench_web.app import create_app
from aisbench_web.db import Database
from aisbench_web.jobs.states import JobStatus
from aisbench_web.repositories.jobs import JobRepository
from aisbench_web.settings import Settings
from conftest import TEST_PASSWORD, ClientFactory

ENDPOINT_PAYLOAD = {
    "name": "qwen",
    "base_url": "http://127.0.0.1:8001/v1",
    "model_name": "Qwen3-32B",
    "api_key": "secret-token",
    "request_timeout": 60,
    "max_output_length": 512,
}
ACCURACY_JOB = {
    "dataset_id": "gsm8k",
    "mode": "accuracy",
    "parameters": {"num_prompts": 8, "max_num_workers": 1},
}


@pytest.fixture
def datasets_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ais_bench" / "datasets"
    (root / "gsm8k").mkdir(parents=True)
    # Installed, but the installed AISBench ships no mmlu *_perf config.
    (root / "mmlu").mkdir()
    monkeypatch.setenv("AISBENCH_DATASETS_DIR", str(root))
    return root


@pytest.fixture
def api_app(settings: Settings, datasets_root: Path) -> FastAPI:
    return create_app(settings=settings, start_worker=False)


@pytest_asyncio.fixture
async def owner(client_factory: ClientFactory, database: Database):
    client = await client_factory("alice")
    endpoint = (await client.post("/api/models", json=ENDPOINT_PAYLOAD)).json()
    return SimpleNamespace(
        client=client,
        endpoint_id=endpoint["id"],
        user_id=(await client.get("/api/me")).json()["id"],
        jobs=JobRepository(database),
    )


async def submit(owner, **overrides) -> httpx.Response:
    payload = {**ACCURACY_JOB, "model_endpoint_id": owner.endpoint_id, **overrides}
    return await owner.client.post("/api/jobs", json=payload)


# --- creation ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_job_uses_current_user_and_returns_queue_position(owner) -> None:
    response = await submit(owner)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["queue_position"] == 1
    assert body["mode"] == "accuracy"
    second = await submit(owner)
    assert second.json()["queue_position"] == 2


@pytest.mark.asyncio
async def test_created_job_keeps_display_snapshots_when_the_endpoint_changes(owner) -> None:
    job_id = (await submit(owner)).json()["id"]

    await owner.client.patch(
        f"/api/models/{owner.endpoint_id}",
        json={"name": "renamed", "model_name": "OtherModel"},
    )

    detail = (await owner.client.get(f"/api/jobs/{job_id}")).json()
    assert detail["model"]["model_name"] == "Qwen3-32B"
    assert detail["dataset"]["name"] == "GSM8K"


@pytest.mark.asyncio
async def test_the_client_cannot_choose_an_owner(owner) -> None:
    response = await submit(owner, owner_id="somebody-else")

    assert response.status_code == 201
    stored = owner.jobs.get_for_owner(response.json()["id"], owner.user_id)
    assert stored is not None


@pytest.mark.asyncio
async def test_another_users_endpoint_cannot_be_used(client_factory: ClientFactory, owner) -> None:
    bob = await client_factory("bob")

    response = await bob.post(
        "/api/jobs",
        json={**ACCURACY_JOB, "model_endpoint_id": owner.endpoint_id},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters",
    [
        {"num_prompts": 0},
        {"num_prompts": 1000001},
        {"max_num_workers": 0},
        {"max_num_workers": 129},
        {"max_output_length": 0},
    ],
)
async def test_out_of_range_accuracy_parameters_are_refused(owner, parameters: dict) -> None:
    response = await submit(owner, parameters={**ACCURACY_JOB["parameters"], **parameters})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_performance_parameters_are_validated_separately(owner) -> None:
    good = await submit(
        owner,
        mode="performance",
        parameters={"num_prompts": 32, "concurrency": 4, "stream": True, "visualization": True},
    )
    bad = await submit(owner, mode="performance", parameters={"num_prompts": 32, "concurrency": 0})

    assert good.status_code == 201
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_a_dataset_without_a_config_for_the_mode_is_refused(owner) -> None:
    response = await submit(owner, dataset_id="mmlu", mode="performance")

    assert response.status_code == 409
    assert "performance" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_uninstalled_dataset_is_refused(owner) -> None:
    response = await submit(owner, dataset_id="ceval")

    assert response.status_code == 409
    assert "install" in response.json()["detail"]


# --- listing and ownership ---------------------------------------------------


@pytest.mark.asyncio
async def test_listing_shows_only_the_current_users_jobs(
    client_factory: ClientFactory,
    owner,
) -> None:
    mine = (await submit(owner)).json()["id"]
    bob = await client_factory("bob")

    assert [job["id"] for job in (await owner.client.get("/api/jobs")).json()] == [mine]
    assert (await bob.get("/api/jobs")).json() == []
    assert (await bob.get(f"/api/jobs/{mine}")).status_code == 404


@pytest.mark.asyncio
async def test_user_cannot_cancel_other_users_job(client_factory: ClientFactory, owner) -> None:
    job_id = (await submit(owner)).json()["id"]
    bob = await client_factory("bob")

    assert (await bob.post(f"/api/jobs/{job_id}/cancel")).status_code == 404
    assert owner.jobs.get_for_owner(job_id, owner.user_id).status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_cancelling_queued_work_is_immediate_and_running_work_is_asked(owner) -> None:
    running = (await submit(owner)).json()["id"]
    owner.jobs.claim_next()
    owner.jobs.transition(running, JobStatus.RUNNING, pid=1)
    queued = (await submit(owner)).json()["id"]

    cancelled = await owner.client.post(f"/api/jobs/{queued}/cancel")
    stopping = await owner.client.post(f"/api/jobs/{running}/cancel")

    assert cancelled.json()["status"] == "cancelled"
    assert stopping.json()["status"] == "stopping"


@pytest.mark.asyncio
async def test_cancelling_a_finished_job_is_refused(owner) -> None:
    job_id = (await submit(owner)).json()["id"]
    owner.jobs.claim_next()
    owner.jobs.transition(job_id, JobStatus.RUNNING, pid=1)
    owner.jobs.transition(job_id, JobStatus.SUCCEEDED, exit_code=0)

    assert (await owner.client.post(f"/api/jobs/{job_id}/cancel")).status_code == 409


# --- logs --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_offset_returns_only_new_bytes(owner, settings: Settings) -> None:
    job_id = (await submit(owner)).json()["id"]
    job = owner.jobs.get_for_owner(job_id, owner.user_id)
    log_path = settings.jobs_dir / job.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("first\nsecond\n", encoding="utf-8")

    response = await owner.client.get(f"/api/jobs/{job_id}/logs?offset=6")

    assert response.json() == {"offset": 13, "text": "second\n"}


@pytest.mark.asyncio
async def test_missing_log_reads_as_empty_at_offset_zero(owner) -> None:
    job_id = (await submit(owner)).json()["id"]

    response = await owner.client.get(f"/api/jobs/{job_id}/logs")

    assert response.json() == {"offset": 0, "text": ""}


@pytest.mark.asyncio
async def test_log_response_is_capped_and_reports_the_resumable_offset(
    owner,
    settings: Settings,
) -> None:
    job_id = (await submit(owner)).json()["id"]
    job = owner.jobs.get_for_owner(job_id, owner.user_id)
    log_path = settings.jobs_dir / job.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"x" * (300 * 1024))

    response = (await owner.client.get(f"/api/jobs/{job_id}/logs")).json()

    assert response["offset"] == 256 * 1024
    assert len(response["text"]) == 256 * 1024
    rest = (await owner.client.get(f"/api/jobs/{job_id}/logs?offset=262144")).json()
    assert rest["offset"] == 300 * 1024


@pytest.mark.asyncio
async def test_invalid_bytes_in_the_log_do_not_break_the_response(
    owner,
    settings: Settings,
) -> None:
    job_id = (await submit(owner)).json()["id"]
    job = owner.jobs.get_for_owner(job_id, owner.user_id)
    log_path = settings.jobs_dir / job.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"ok \xff\xfe done\n")

    response = (await owner.client.get(f"/api/jobs/{job_id}/logs")).json()

    assert "ok" in response["text"] and "done" in response["text"]


@pytest.mark.asyncio
async def test_logs_are_owner_scoped(client_factory: ClientFactory, owner) -> None:
    job_id = (await submit(owner)).json()["id"]
    bob = await client_factory("bob")

    assert (await bob.get(f"/api/jobs/{job_id}/logs")).status_code == 404


@pytest.mark.asyncio
async def test_job_endpoints_require_authentication(anonymous_client: httpx.AsyncClient) -> None:
    assert (await anonymous_client.get("/api/jobs")).status_code == 401
    assert (await anonymous_client.post("/api/jobs", json=ACCURACY_JOB)).status_code == 401
    assert (await anonymous_client.get("/api/jobs/x/logs")).status_code == 401
    assert (await anonymous_client.post("/api/jobs/x/cancel")).status_code == 401


# --- websocket ---------------------------------------------------------------


def test_websocket_requires_a_session_and_ownership(api_app: FastAPI) -> None:
    from starlette.websockets import WebSocketDisconnect

    with TestClient(api_app) as client:
        client.post("/api/auth/register", json={"username": "alice", "password": TEST_PASSWORD})
        endpoint = client.post("/api/models", json=ENDPOINT_PAYLOAD).json()
        job = client.post(
            "/api/jobs", json={**ACCURACY_JOB, "model_endpoint_id": endpoint["id"]}
        ).json()

        with client.websocket_connect(f"/ws/jobs/{job['id']}") as websocket:
            api_app.state.notifier.publish(job["id"], {"type": "status", "status": "running"})
            assert websocket.receive_json() == {"type": "status", "status": "running"}

        client.post("/api/auth/logout")
        # Assert on the handshake, never on a receive: if a regression accepts the socket,
        # this fails immediately instead of blocking forever on a message that never comes.
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/ws/jobs/{job['id']}"),
        ):
            pass


def test_websocket_refuses_another_users_job(api_app: FastAPI) -> None:
    from starlette.websockets import WebSocketDisconnect

    with TestClient(api_app) as client:
        client.post("/api/auth/register", json={"username": "alice", "password": TEST_PASSWORD})
        endpoint = client.post("/api/models", json=ENDPOINT_PAYLOAD).json()
        job = client.post(
            "/api/jobs", json={**ACCURACY_JOB, "model_endpoint_id": endpoint["id"]}
        ).json()
        client.post("/api/auth/logout")
        client.post("/api/auth/register", json={"username": "bob", "password": TEST_PASSWORD})

        # Assert on the handshake, never on a receive: if a regression accepts the socket,
        # this fails immediately instead of blocking forever on a message that never comes.
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/ws/jobs/{job['id']}"),
        ):
            pass


def test_a_disconnected_subscriber_is_removed(api_app: FastAPI) -> None:
    with TestClient(api_app) as client:
        client.post("/api/auth/register", json={"username": "alice", "password": TEST_PASSWORD})
        endpoint = client.post("/api/models", json=ENDPOINT_PAYLOAD).json()
        job = client.post(
            "/api/jobs", json={**ACCURACY_JOB, "model_endpoint_id": endpoint["id"]}
        ).json()

        with client.websocket_connect(f"/ws/jobs/{job['id']}"):
            assert api_app.state.notifier.subscriber_count(job["id"]) == 1

        # Publishing to nobody must not raise or accumulate queues.
        api_app.state.notifier.publish(job["id"], {"type": "status", "status": "queued"})
        assert api_app.state.notifier.subscriber_count(job["id"]) == 0

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.testclient import TestClient

from aisbench_web.app import create_app
from aisbench_web.db import Database
from aisbench_web.jobs.dataset_progress import DatasetStatus
from aisbench_web.jobs.states import JobStatus
from aisbench_web.repositories.jobs import JobRepository
from aisbench_web.settings import Settings
from conftest import TEST_PASSWORD, ClientFactory

ENDPOINT_PAYLOAD = {
    "name": "qwen",
    "base_url": "http://127.0.0.1:8001/v1",
    "api_key": "secret-token",
}
MODEL_LISTING = {"data": [{"id": "Qwen3-32B"}]}
ACCURACY_JOB = {
    "dataset_ids": ["gsm8k"],
    "mode": "accuracy",
    "parameters": {"cli": {"num_prompts": 8, "max_num_workers": 1}},
}


@pytest.fixture
def datasets_root(
    aisbench_configs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = tmp_path / "installed-datasets"
    (root / "gsm8k").mkdir(parents=True)
    # Installed, but the stand-in AISBench ships no mmlu *_perf config.
    (root / "mmlu").mkdir()
    monkeypatch.setenv("AISBENCH_DATASETS_DIR", str(root))
    return root


@pytest.fixture
def api_app(settings: Settings, datasets_root: Path) -> FastAPI:
    app = create_app(settings=settings, start_worker=False)
    # Creating an endpoint asks the service which model it serves.
    app.state.http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=MODEL_LISTING)
    )
    return app


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
async def test_detail_reports_persisted_progress_without_a_socket(owner) -> None:
    job_id = (await submit(owner)).json()["id"]

    assert (await owner.client.get(f"/api/jobs/{job_id}")).json()["progress"] is None

    owner.jobs.record_progress(job_id, 5, 8)

    assert (await owner.client.get(f"/api/jobs/{job_id}")).json()["progress"] == {
        "completed": 5,
        "total": 8,
    }


@pytest.mark.asyncio
async def test_created_job_keeps_display_snapshots_when_the_endpoint_changes(owner) -> None:
    job_id = (await submit(owner)).json()["id"]

    await owner.client.patch(
        f"/api/models/{owner.endpoint_id}",
        json={"name": "renamed", "model_name": "OtherModel"},
    )

    detail = (await owner.client.get(f"/api/jobs/{job_id}")).json()
    assert detail["model"]["model_name"] == "Qwen3-32B"
    assert detail["dataset"]["name"] == "gsm8k"


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
    "cli",
    [
        {"num_prompts": 0},
        {"num_prompts": 1000001},
        {"max_num_workers": 0},
        {"max_num_workers": 129},
        {"num_warmups": -1},
        # A performance-only option is not an accuracy option, even though both are CLI flags.
        {"pressure": True},
    ],
)
async def test_out_of_range_accuracy_parameters_are_refused(owner, cli: dict) -> None:
    response = await submit(owner, parameters={"cli": {**ACCURACY_JOB["parameters"]["cli"], **cli}})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_config_fields_the_chosen_model_config_does_not_have_are_refused(owner) -> None:
    """The form offers the fields of the selected file; anything else never came from it."""
    response = await submit(
        owner,
        model_config_name="vllm_api_stream_chat",
        parameters={"config_fields": {"returns_tool_calls": True}},
    )

    assert response.status_code == 409
    assert "returns_tool_calls" in response.json()["detail"]


@pytest.mark.asyncio
async def test_performance_parameters_are_validated_separately(owner) -> None:
    good = await submit(
        owner,
        mode="performance",
        parameters={
            "cli": {"num_prompts": 32, "pressure": True, "pressure_time": 30},
            "config_fields": {"batch_size": 4},
            "generation_kwargs": {"temperature": 0.7},
        },
    )
    bad = await submit(
        owner, mode="performance", parameters={"cli": {"num_prompts": 32, "pressure_time": 0}}
    )

    assert good.status_code == 201
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_a_dataset_without_a_config_for_the_mode_is_refused(owner) -> None:
    response = await submit(owner, dataset_ids=["mmlu"], mode="performance")

    assert response.status_code == 409
    assert "performance" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_uninstalled_dataset_is_refused(owner) -> None:
    response = await submit(owner, dataset_ids=["synthetic"])

    assert response.status_code == 409
    assert "install" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_job_can_combine_several_datasets(owner) -> None:
    response = await submit(owner, dataset_ids=["gsm8k", "mmlu"])

    assert response.status_code == 201
    body = response.json()
    # The display column carries the first dataset; the snapshot carries the rest.
    assert body["dataset"]["name"] == "gsm8k"
    assert body["name"] == "gsm8k +1"
    stored = owner.jobs.get_for_owner(body["id"], owner.user_id)
    entries = stored.dataset_snapshot["datasets"]
    assert [entry["name"] for entry in entries] == ["gsm8k", "mmlu"]
    assert entries[0]["abbr"] == "gsm8k"


@pytest.mark.asyncio
async def test_a_created_job_already_lists_its_datasets_as_queued(owner) -> None:
    """The page opens straight after submitting; the rows must already be there."""
    body = (await submit(owner, dataset_ids=["gsm8k", "mmlu"])).json()

    detail = (await owner.client.get(f"/api/jobs/{body['id']}")).json()

    assert [(row["name"], row["phase"]) for row in detail["datasets"]] == [
        ("gsm8k", "queued"),
        ("mmlu", "queued"),
    ]


@pytest.mark.asyncio
async def test_a_dataset_the_job_does_not_include_cannot_pick_its_config(owner) -> None:
    response = await submit(owner, config_names={"mmlu": "mmlu_gen_5_shot_chat_prompt"})

    assert response.status_code == 409
    assert "does not include" in response.json()["detail"]


@pytest.mark.asyncio
async def test_the_same_config_cannot_be_chosen_twice(owner) -> None:
    """Two catalog entries can point at one config file; running it twice helps nobody."""
    response = await submit(
        owner,
        dataset_ids=["gsm8k", "gsm8k"],
        config_names={"gsm8k": "gsm8k_gen_4_shot_cot_chat_prompt"},
    )

    assert response.status_code == 201  # a repeated id is the same choice, not two


@pytest.mark.asyncio
async def test_detail_lists_dataset_rows_in_the_order_the_job_was_configured(owner) -> None:
    job_id = (await submit(owner, dataset_ids=["gsm8k", "mmlu"])).json()["id"]

    owner.jobs.replace_dataset_progress(
        job_id,
        [
            DatasetStatus(dataset="mmlu", phase="inferring", completed=1, total=4),
            DatasetStatus(dataset="gsm8k", phase="finished", completed=8, total=8),
        ],
    )

    detail = (await owner.client.get(f"/api/jobs/{job_id}")).json()
    assert [(row["name"], row["phase"]) for row in detail["datasets"]] == [
        ("gsm8k", "finished"),
        ("mmlu", "inferring"),
    ]
    assert detail["datasets"][1]["completed"] == 1


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


# --- one dataset's own log ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_datasets_log_is_served_by_the_name_progress_reports(owner, settings) -> None:
    job_id = (await submit(owner)).json()["id"]
    job = owner.jobs.get_for_owner(job_id, owner.user_id)
    run_dir = settings.jobs_dir / job.output_dir / "20260819_000000"
    run_dir.mkdir(parents=True)
    (run_dir / "infer.log").write_text("inferring gsm8k\n", encoding="utf-8")
    owner.jobs.replace_dataset_progress(
        job_id,
        [
            DatasetStatus(
                dataset="gsm8k",
                phase="inferring",
                completed=3,
                total=8,
                log_path="outputs/20260819_000000/infer.log",
            )
        ],
    )

    response = await owner.client.get(f"/api/jobs/{job_id}/datasets/gsm8k/logs")

    assert response.json() == {"offset": 16, "text": "inferring gsm8k\n"}


@pytest.mark.asyncio
async def test_a_dataset_name_is_matched_not_turned_into_a_path(owner) -> None:
    """The name addresses a stored row; a traversal attempt simply matches nothing."""
    job_id = (await submit(owner)).json()["id"]

    assert (
        await owner.client.get(f"/api/jobs/{job_id}/datasets/..%2F..%2Fetc%2Fpasswd/logs")
    ).status_code == 404
    # The queued row creation seeded carries no log yet, so the name matches and the
    # log reads empty — the run has not produced one.
    assert (
        await owner.client.get(f"/api/jobs/{job_id}/datasets/gsm8k/logs")
    ).json() == {"offset": 0, "text": ""}


@pytest.mark.asyncio
async def test_a_dataset_without_a_log_reads_as_empty(owner) -> None:
    job_id = (await submit(owner)).json()["id"]
    owner.jobs.replace_dataset_progress(
        job_id, [DatasetStatus(dataset="gsm8k", phase="inferring")]
    )

    response = await owner.client.get(f"/api/jobs/{job_id}/datasets/gsm8k/logs")

    assert response.json() == {"offset": 0, "text": ""}


@pytest.mark.asyncio
async def test_a_datasets_samples_are_paged_from_the_evaluator_details(
    owner, settings
) -> None:
    import json as json_module

    job_id = (await submit(owner)).json()["id"]
    job = owner.jobs.get_for_owner(job_id, owner.user_id)
    results_dir = settings.jobs_dir / job.output_dir / "20260819_000000" / "results" / "qwen"
    results_dir.mkdir(parents=True)
    (results_dir / "gsm8k.json").write_text(
        json_module.dumps(
            {
                "accuracy": 50.0,
                "details": {
                    str(index): {
                        "prompt": [{"role": "HUMAN", "prompt": f"Question {index}"}],
                        "origin_prediction": f"raw {index}",
                        "predictions": f"answer {index}",
                        "references": f"gold {index}",
                        "correct": [True],
                    }
                    for index in range(3)
                },
            }
        ),
        encoding="utf-8",
    )
    owner.jobs.replace_dataset_progress(
        job_id, [DatasetStatus(dataset="gsm8k", phase="finished")]
    )

    page = await owner.client.get(f"/api/jobs/{job_id}/datasets/gsm8k/samples?limit=2")

    body = page.json()
    assert body["source"] == "eval_details"
    assert body["total"] == 3
    assert [sample["id"] for sample in body["samples"]] == ["0", "1"]

    rest = await owner.client.get(
        f"/api/jobs/{job_id}/datasets/gsm8k/samples?offset=2&limit=2"
    )
    assert [sample["id"] for sample in rest.json()["samples"]] == ["2"]
    # A name no row carries is not a dataset of this job, whatever it spells.
    assert (
        await owner.client.get(f"/api/jobs/{job_id}/datasets/..%2Fetc/logs")
    ).status_code == 404


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

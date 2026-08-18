import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from aisbench_web.db import Database
from aisbench_web.jobs.results import (
    index_artifacts,
    parse_accuracy,
    parse_performance,
    parse_results,
    safe_artifact_path,
)
from aisbench_web.jobs.states import JobStatus
from aisbench_web.repositories.jobs import JobRepository
from aisbench_web.settings import Settings
from conftest import ClientFactory

ACCURACY_CSV = "dataset,version,metric,mode,qwen\ngsm8k,1,accuracy,gen,82.5\n"
# Shape and key names taken from AISBench's DefaultPerfMetricCalculator: nested
# {metric: {stage: value}} where recognised values carry their unit as a suffix.
PERFORMANCE_JSON = {
    "Benchmark Duration": {"total": "12000.0 ms"},
    "Total Requests": {"total": 32},
    "Success Requests": {"total": 30},
    "Failed Requests": {"total": 2},
    "Request Throughput": {"total": "2.5 req/s"},
    "Output Token Throughput": {"total": "480.25 token/s"},
    "E2EL": {"Average": "1500.5 ms", "P90": "2100.0 ms"},
    "TTFT": {"Average": "120.25 ms"},
    "TPOT": {"Average": "35.5 ms"},
    "SomeFutureMetric": {"total": 7},
    "AStringMetric": {"total": "not a number"},
}


def write_accuracy_output(output_dir: Path, csv_text: str = ACCURACY_CSV) -> Path:
    summary = output_dir / "summary"
    summary.mkdir(parents=True, exist_ok=True)
    path = summary / "summary_20260817_120000.csv"
    path.write_text(csv_text, encoding="utf-8")
    return path


def write_performance_output(output_dir: Path, payload: dict | None = None) -> Path:
    directory = output_dir / "performances" / "job-model"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gsm8k.json"
    path.write_text(json.dumps(payload or PERFORMANCE_JSON), encoding="utf-8")
    (directory / "gsm8k_details.jsonl").write_text(
        '{"request_id": 1, "e2el": 1.4}\n', encoding="utf-8"
    )
    return path


# --- accuracy ----------------------------------------------------------------


def test_accuracy_summary_is_normalized(tmp_path: Path) -> None:
    write_accuracy_output(tmp_path)

    result = parse_accuracy(tmp_path)

    assert result.metrics["gsm8k.accuracy"].value == 82.5


def test_accuracy_reads_the_newest_summary(tmp_path: Path) -> None:
    summary = tmp_path / "summary"
    summary.mkdir()
    (summary / "summary_20260101_000000.csv").write_text(
        "dataset,version,metric,mode,qwen\ngsm8k,1,accuracy,gen,10.0\n", encoding="utf-8"
    )
    (summary / "summary_20260817_120000.csv").write_text(ACCURACY_CSV, encoding="utf-8")

    assert parse_accuracy(tmp_path).metrics["gsm8k.accuracy"].value == 82.5


def test_accuracy_keeps_every_dataset_and_metric_row(tmp_path: Path) -> None:
    write_accuracy_output(
        tmp_path,
        "dataset,version,metric,mode,qwen\n"
        "gsm8k,1,accuracy,gen,82.5\n"
        "gsm8k,1,pass@1,gen,64.0\n"
        "ceval,1,accuracy,gen,71.25\n",
    )

    metrics = parse_accuracy(tmp_path).metrics

    assert metrics["gsm8k.accuracy"].value == 82.5
    assert metrics["gsm8k.pass@1"].value == 64.0
    assert metrics["ceval.accuracy"].value == 71.25


def test_accuracy_keeps_non_numeric_cells_as_text(tmp_path: Path) -> None:
    write_accuracy_output(
        tmp_path, "dataset,version,metric,mode,qwen\ngsm8k,1,accuracy,gen,-\n"
    )

    metric = parse_accuracy(tmp_path).metrics["gsm8k.accuracy"]

    assert metric.value is None
    assert metric.text_value == "-"


def test_missing_accuracy_summary_warns_instead_of_failing(tmp_path: Path) -> None:
    result = parse_accuracy(tmp_path)

    assert result.metrics == {}
    assert result.warnings


# --- performance -------------------------------------------------------------


def test_performance_metrics_are_normalized_with_units(tmp_path: Path) -> None:
    write_performance_output(tmp_path)

    metrics = parse_performance(tmp_path).metrics

    assert metrics["latency.ttft.Average"].value == 120.25
    assert metrics["latency.ttft.Average"].unit == "ms"
    assert metrics["latency.e2e.P90"].value == 2100.0
    assert metrics["latency.tpot.Average"].value == 35.5
    assert metrics["throughput.output_tokens.total"].value == 480.25
    assert metrics["throughput.output_tokens.total"].unit == "token/s"
    assert metrics["throughput.requests.total"].unit == "req/s"
    assert metrics["requests.succeeded.total"].value == 30
    assert metrics["requests.failed.total"].value == 2


def test_unknown_performance_metrics_are_preserved_not_dropped(tmp_path: Path) -> None:
    write_performance_output(tmp_path)

    metrics = parse_performance(tmp_path).metrics

    assert metrics["extra.SomeFutureMetric.total"].value == 7
    assert metrics["extra.AStringMetric.total"].text_value == "not a number"
    assert metrics["extra.AStringMetric.total"].value is None


def test_performance_tolerates_a_dataset_without_the_usual_metrics(tmp_path: Path) -> None:
    write_performance_output(tmp_path, {"OnlyThis": {"total": 1}})

    result = parse_performance(tmp_path)

    assert result.metrics["extra.OnlyThis.total"].value == 1
    assert not any("OnlyThis" in warning for warning in result.warnings)


def test_missing_performance_output_warns_instead_of_failing(tmp_path: Path) -> None:
    result = parse_performance(tmp_path)

    assert result.metrics == {}
    assert result.warnings


def test_parse_results_routes_by_mode(tmp_path: Path) -> None:
    write_accuracy_output(tmp_path)
    write_performance_output(tmp_path)

    assert "gsm8k.accuracy" in parse_results("accuracy", tmp_path).metrics
    assert "latency.ttft.Average" in parse_results("performance", tmp_path).metrics


# --- artifacts ---------------------------------------------------------------


def test_artifact_path_cannot_escape_job_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="artifact escapes job directory"):
        safe_artifact_path(tmp_path / "job", "../secret")


@pytest.mark.parametrize(
    "relative",
    ["../secret", "/etc/passwd", "a/../../secret", "", "outputs/../../secret"],
)
def test_artifact_paths_outside_the_job_are_refused(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError, match="artifact escapes job directory"):
        safe_artifact_path(tmp_path / "job", relative)


def test_artifact_symlink_out_of_the_job_is_refused(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    (job_dir / "link").symlink_to(secret)

    with pytest.raises(ValueError, match="artifact escapes job directory"):
        safe_artifact_path(job_dir, "link")


def test_artifacts_are_indexed_with_kinds_and_relative_paths(tmp_path: Path) -> None:
    write_accuracy_output(tmp_path)
    write_performance_output(tmp_path)
    (tmp_path / "predictions").mkdir()
    (tmp_path / "predictions" / "gsm8k.json").write_text("[]", encoding="utf-8")
    (tmp_path / "report.html").write_text("<html></html>", encoding="utf-8")

    indexed = {artifact.relative_path: artifact for artifact in index_artifacts(tmp_path)}

    assert indexed["summary/summary_20260817_120000.csv"].kind == "summary"
    assert indexed["summary/summary_20260817_120000.csv"].content_type == "text/csv"
    assert indexed["predictions/gsm8k.json"].kind == "prediction"
    assert indexed["performances/job-model/gsm8k.json"].kind == "performance"
    assert indexed["report.html"].kind == "visualization"
    assert indexed["report.html"].content_type == "text/html"
    assert all(not Path(path).is_absolute() for path in indexed)


# --- comparison and download API ---------------------------------------------


@pytest_asyncio.fixture
async def owners(client_factory: ClientFactory, database: Database, settings: Settings):
    """Two registered users with succeeded jobs, stored metrics, and one artifact each."""
    alice = await client_factory("alice")
    bob = await client_factory("bob")
    identities = {
        "alice": (await alice.get("/api/me")).json()["id"],
        "bob": (await bob.get("/api/me")).json()["id"],
    }
    repository = JobRepository(database)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO model_endpoints (
              id, owner_id, name, base_url, model_name, created_at, updated_at
            ) VALUES ('endpoint-1', ?, 'q', 'http://h/v1', 'Qwen3-32B', 'c', 'u')
            """,
            (identities["alice"],),
        )
        # The catalog sync in the app lifespan already seeded the shared gsm8k row.
        connection.execute(
            """
            INSERT OR IGNORE INTO datasets (
              id, config_name, name, description, status, updated_at
            ) VALUES ('gsm8k', 'gsm8k_gen', 'GSM8K', 'math', 'available', 'u')
            """
        )

    jobs: dict[str, list] = {"alice": [], "bob": []}
    for who, count in (("alice", 2), ("bob", 1)):
        for index in range(count):
            job = repository.create(
                owner_id=identities[who],
                model_endpoint_id="endpoint-1",
                dataset_id="gsm8k",
                mode="accuracy",
                parameters={},
                model_snapshot={"model_name": "Qwen3-32B", "base_url": "http://h/v1"},
                dataset_snapshot={"id": "gsm8k", "name": "GSM8K"},
            )
            repository.claim_next()
            repository.transition(job.id, JobStatus.RUNNING, pid=1)
            repository.transition(job.id, JobStatus.SUCCEEDED, exit_code=0)
            repository.replace_metrics(job.id, {"gsm8k.accuracy": (82.5 + index, None, None)})
            summary_dir = settings.jobs_dir / job.output_dir / "summary"
            summary_dir.mkdir(parents=True, exist_ok=True)
            (summary_dir / "summary_1.csv").write_text(ACCURACY_CSV, encoding="utf-8")
            repository.replace_artifacts(
                job.id, [("summary", "summary/summary_1.csv", "text/csv")]
            )
            jobs[who].append(job)

    return SimpleNamespace(
        alice=alice,
        bob=bob,
        alice_jobs=[job.id for job in jobs["alice"]],
        bob_jobs=[job.id for job in jobs["bob"]],
    )


@pytest.mark.asyncio
async def test_comparison_returns_aligned_rows_for_owned_jobs(owners) -> None:
    response = await owners.alice.post("/api/comparisons", json={"job_ids": owners.alice_jobs})

    assert response.status_code == 200
    body = response.json()
    assert [job["id"] for job in body["jobs"]] == owners.alice_jobs
    assert body["jobs"][0]["model"] == "Qwen3-32B"
    assert body["jobs"][0]["dataset"] == "GSM8K"
    row = next(row for row in body["rows"] if row["key"] == "gsm8k.accuracy")
    assert row["values"] == {owners.alice_jobs[0]: 82.5, owners.alice_jobs[1]: 83.5}


@pytest.mark.asyncio
async def test_comparison_rejects_another_users_job(owners) -> None:
    response = await owners.alice.post(
        "/api/comparisons",
        json={"job_ids": [owners.alice_jobs[0], owners.bob_jobs[0]]},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_comparison_requires_between_two_and_eight_jobs(owners) -> None:
    one = {"job_ids": owners.alice_jobs[:1]}
    nine = {"job_ids": [owners.alice_jobs[0]] * 9}

    assert (await owners.alice.post("/api/comparisons", json=one)).status_code == 422
    assert (await owners.alice.post("/api/comparisons", json=nine)).status_code == 422


@pytest.mark.asyncio
async def test_comparison_marks_incompatible_jobs_instead_of_refusing(
    owners,
    database: Database,
) -> None:
    with database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET mode = 'performance' WHERE id = ?", (owners.alice_jobs[1],)
        )

    body = (
        await owners.alice.post("/api/comparisons", json={"job_ids": owners.alice_jobs})
    ).json()

    assert body["warnings"]
    assert [job["id"] for job in body["jobs"]] == owners.alice_jobs


@pytest.mark.asyncio
async def test_unfinished_jobs_cannot_be_compared(owners, database: Database) -> None:
    with database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (owners.alice_jobs[1],)
        )

    response = await owners.alice.post("/api/comparisons", json={"job_ids": owners.alice_jobs})

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_artifacts_download_by_id_and_never_by_path(owners) -> None:
    job_id = owners.alice_jobs[0]
    listed = await owners.alice.get(f"/api/jobs/{job_id}/artifacts")

    assert listed.status_code == 200
    assert listed.json()[0]["relative_path"] == "summary/summary_1.csv"
    artifact_id = listed.json()[0]["id"]

    downloaded = await owners.alice.get(f"/api/jobs/{job_id}/artifacts/{artifact_id}")

    assert downloaded.status_code == 200
    assert "gsm8k" in downloaded.text
    # A path where an ID belongs must not resolve, even to a file that really exists.
    escaped = await owners.alice.get(
        f"/api/jobs/{job_id}/artifacts/summary%2Fsummary_1.csv"
    )
    assert escaped.status_code == 404


@pytest.mark.asyncio
async def test_artifacts_are_owner_scoped(owners) -> None:
    job_id = owners.alice_jobs[0]
    artifact_id = (await owners.alice.get(f"/api/jobs/{job_id}/artifacts")).json()[0]["id"]

    assert (await owners.bob.get(f"/api/jobs/{job_id}/artifacts")).status_code == 404
    assert (
        await owners.bob.get(f"/api/jobs/{job_id}/artifacts/{artifact_id}")
    ).status_code == 404


@pytest.mark.asyncio
async def test_result_endpoints_require_authentication(
    anonymous_client: httpx.AsyncClient,
) -> None:
    assert (
        await anonymous_client.post("/api/comparisons", json={"job_ids": ["a", "b"]})
    ).status_code == 401
    assert (await anonymous_client.get("/api/jobs/any/artifacts")).status_code == 401

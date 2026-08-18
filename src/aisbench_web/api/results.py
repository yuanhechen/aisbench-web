import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from aisbench_web.dependencies import get_current_user
from aisbench_web.jobs.results import safe_artifact_path
from aisbench_web.jobs.states import JobStatus
from aisbench_web.repositories.jobs import Job, JobRepository
from aisbench_web.repositories.users import User
from aisbench_web.settings import Settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MIN_COMPARED_JOBS = 2
MAX_COMPARED_JOBS = 8
JOB_NOT_FOUND = "job not found"
ARTIFACT_NOT_FOUND = "artifact not found"


class ComparisonRequest(BaseModel):
    job_ids: list[str] = Field(min_length=MIN_COMPARED_JOBS, max_length=MAX_COMPARED_JOBS)


class ComparedJob(BaseModel):
    id: str
    mode: str
    model: str
    dataset: str


class ComparisonRow(BaseModel):
    key: str
    unit: str | None
    values: dict[str, float | None]


class ComparisonResponse(BaseModel):
    jobs: list[ComparedJob]
    rows: list[ComparisonRow]
    warnings: list[str]


class ArtifactResponse(BaseModel):
    id: str
    kind: str
    relative_path: str
    content_type: str


class MetricResponse(BaseModel):
    key: str
    value: float | None
    text_value: str | None
    unit: str | None


def get_job_repository(request: Request) -> JobRepository:
    return JobRepository(request.app.state.database)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


RepositoryDependency = Annotated[JobRepository, Depends(get_job_repository)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
CurrentUserDependency = Annotated[User, Depends(get_current_user)]


def _owned_job_or_404(repository: JobRepository, job_id: str, owner_id: str) -> Job:
    job = repository.get_for_owner(job_id, owner_id)
    if job is None:
        # The same 404 an unknown ID gets: owning an ID must reveal nothing.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND)
    return job


@router.post("/comparisons", response_model=ComparisonResponse)
def compare_jobs(
    payload: ComparisonRequest,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> ComparisonResponse:
    """Comparisons are computed per request; the MVP stores no comparison objects."""
    jobs = [_owned_job_or_404(repository, job_id, user.id) for job_id in payload.job_ids]

    unfinished = [job.id for job in jobs if job.status != JobStatus.SUCCEEDED.value]
    if unfinished:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"only succeeded jobs can be compared; {len(unfinished)} have not succeeded",
        )

    warnings: list[str] = []
    modes = {job.mode for job in jobs}
    if len(modes) > 1:
        warnings.append(
            "These jobs use different modes (" + ", ".join(sorted(modes)) + "), "
            "so their metrics are not directly comparable."
        )
    datasets = {job.dataset_snapshot.get("id") for job in jobs}
    if len(datasets) > 1:
        warnings.append("These jobs use different datasets, so scores measure different work.")

    rows: dict[str, dict[str, float | None]] = {}
    units: dict[str, str | None] = {}
    for job in jobs:
        for metric in repository.list_metrics_for_owner(job.id, user.id):
            rows.setdefault(metric.key, {})[job.id] = metric.value
            units.setdefault(metric.key, metric.unit)

    missing = [key for key, values in rows.items() if len(values) != len(jobs)]
    if missing:
        warnings.append(
            f"{len(missing)} metric(s) are missing from at least one job and are shown blank."
        )

    return ComparisonResponse(
        jobs=[
            ComparedJob(
                id=job.id,
                mode=job.mode,
                model=job.model_snapshot.get("model_name", ""),
                dataset=job.dataset_snapshot.get("name") or job.dataset_snapshot.get("id", ""),
            )
            for job in jobs
        ],
        rows=[
            ComparisonRow(key=key, unit=units.get(key), values=values)
            for key, values in sorted(rows.items())
        ],
        warnings=warnings,
    )


@router.get("/jobs/{job_id}/metrics", response_model=list[MetricResponse])
def list_metrics(
    job_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> list[MetricResponse]:
    _owned_job_or_404(repository, job_id, user.id)
    return [
        MetricResponse(key=m.key, value=m.value, text_value=m.text_value, unit=m.unit)
        for m in repository.list_metrics_for_owner(job_id, user.id)
    ]


@router.get("/jobs/{job_id}/artifacts", response_model=list[ArtifactResponse])
def list_artifacts(
    job_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> list[ArtifactResponse]:
    _owned_job_or_404(repository, job_id, user.id)
    return [
        ArtifactResponse(
            id=artifact.id,
            kind=artifact.kind,
            relative_path=artifact.relative_path,
            content_type=artifact.content_type,
        )
        for artifact in repository.list_artifacts_for_owner(job_id, user.id)
    ]


@router.get("/jobs/{job_id}/artifacts/{artifact_id}")
def download_artifact(
    job_id: str,
    artifact_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
) -> FileResponse:
    job = _owned_job_or_404(repository, job_id, user.id)
    artifact = repository.get_artifact_for_owner(job_id, artifact_id, user.id)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ARTIFACT_NOT_FOUND)

    # The path comes from the database, never the request, and is re-checked against the job
    # directory in case the stored row was ever tampered with.
    try:
        path = safe_artifact_path(settings.jobs_dir / job.output_dir, artifact.relative_path)
    except ValueError:
        logger.error("Artifact %s resolves outside job %s", artifact.id, job.id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=ARTIFACT_NOT_FOUND
        ) from None
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ARTIFACT_NOT_FOUND)
    return FileResponse(
        path,
        media_type=artifact.content_type,
        filename=path.name,
    )

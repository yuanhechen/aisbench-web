import asyncio
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from aisbench_web.datasets.catalog import CatalogEntry, load_catalog
from aisbench_web.dependencies import get_current_user, get_user_repository
from aisbench_web.jobs.states import TERMINAL_STATUSES
from aisbench_web.repositories.datasets import DatasetRepository, DatasetStatus
from aisbench_web.repositories.jobs import Job, JobRepository
from aisbench_web.repositories.models import ModelEndpointRepository
from aisbench_web.repositories.users import User
from aisbench_web.security import SESSION_COOKIE, session_token_digest
from aisbench_web.settings import Settings

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_CHUNK_LIMIT = 256 * 1024
JOB_NOT_FOUND = "job not found"
DEFAULT_MAX_OUTPUT_LENGTH = 512
ACCURACY = "accuracy"
PERFORMANCE = "performance"


class AccuracyParameters(BaseModel):
    num_prompts: int | None = Field(default=None, ge=1, le=1_000_000)
    max_num_workers: int = Field(default=1, ge=1, le=128)
    max_output_length: int | None = Field(default=None, ge=1, le=131072)
    detailed_scoring: bool = False


class PerformanceParameters(BaseModel):
    num_prompts: int | None = Field(default=None, ge=1, le=1_000_000)
    concurrency: int | None = Field(default=None, ge=1, le=4096)
    request_rate: float | None = Field(default=None, ge=0, le=100_000)
    max_output_length: int | None = Field(default=None, ge=1, le=131072)
    stream: bool = True
    visualization: bool = False


PARAMETER_MODELS = {ACCURACY: AccuracyParameters, PERFORMANCE: PerformanceParameters}


class JobCreate(BaseModel):
    model_endpoint_id: str
    dataset_id: str
    mode: Literal["accuracy", "performance"]
    #: A specific AISBench config for this dataset; the first one for the mode when omitted.
    config_name: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelDisplay(BaseModel):
    name: str
    model_name: str
    base_url: str


class DatasetDisplay(BaseModel):
    id: str
    name: str
    config_name: str = ""


class JobProgress(BaseModel):
    completed: int
    total: int


class JobResponse(BaseModel):
    id: str
    mode: str
    status: str
    queue_position: int | None
    progress: JobProgress | None
    model: ModelDisplay
    dataset: DatasetDisplay
    parameters: dict
    exit_code: int | None
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class LogChunk(BaseModel):
    offset: int
    text: str


def get_job_repository(request: Request) -> JobRepository:
    return JobRepository(request.app.state.database)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


RepositoryDependency = Annotated[JobRepository, Depends(get_job_repository)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
CurrentUserDependency = Annotated[User, Depends(get_current_user)]


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND)


def _to_response(job: Job, repository: JobRepository) -> JobResponse:
    model = job.model_snapshot
    dataset = job.dataset_snapshot
    ahead = repository.queue_position(job.id)
    return JobResponse(
        id=job.id,
        mode=job.mode,
        status=job.status,
        # 1-based: the user's own job is position 1 when nothing is ahead of it.
        queue_position=None if ahead is None else ahead + 1,
        progress=(
            None
            if job.progress_total is None or job.progress_completed is None
            else JobProgress(completed=job.progress_completed, total=job.progress_total)
        ),
        model=ModelDisplay(
            name=model.get("name", ""),
            model_name=model.get("model_name", ""),
            base_url=model.get("base_url", ""),
        ),
        dataset=DatasetDisplay(
            id=dataset.get("id", ""),
            name=dataset.get("name", ""),
            config_name=dataset.get("config_name", ""),
        ),
        parameters=job.parameters,
        exit_code=job.exit_code,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _catalog_entry(dataset_id: str) -> CatalogEntry | None:
    return next((entry for entry in load_catalog() if entry.id == dataset_id), None)


@router.post("/api/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreate,
    request: Request,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> JobResponse:
    """Ownership comes from the session; the request body cannot name an owner."""
    try:
        parameters = PARAMETER_MODELS[payload.mode](**payload.parameters).model_dump()
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {key: value for key, value in error.items() if key != "input"}
                for error in exc.errors()
            ],
        ) from exc

    endpoints = ModelEndpointRepository(request.app.state.database)
    endpoint = endpoints.get_for_owner(user.id, payload.model_endpoint_id)
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="model endpoint not found"
        )

    dataset = DatasetRepository(request.app.state.database).get(payload.dataset_id)
    if dataset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dataset not found")
    if dataset.status != DatasetStatus.AVAILABLE.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this dataset is not installed yet; install it before submitting a job",
        )

    entry = _catalog_entry(payload.dataset_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dataset not found")
    config = (
        entry.config_named(payload.config_name)
        if payload.config_name is not None
        else entry.default_config(payload.mode)
    )
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"this dataset has no {payload.mode} configuration in the installed AISBench",
        )
    if config.mode != payload.mode:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"configuration {config.name!r} is a {config.mode} configuration",
        )

    encrypted = endpoints.get_encrypted_api_key_for_owner(user.id, endpoint.id)
    job = repository.create(
        owner_id=user.id,
        model_endpoint_id=endpoint.id,
        dataset_id=dataset.id,
        mode=payload.mode,
        parameters=parameters,
        # Snapshots are display-stable: renaming the endpoint later cannot rewrite history.
        model_snapshot={
            "abbr": endpoint.name,
            "name": endpoint.name,
            "base_url": endpoint.base_url,
            "model_name": endpoint.model_name,
            "max_output_length": parameters.get("max_output_length") or DEFAULT_MAX_OUTPUT_LENGTH,
            "encrypted_api_key": None if encrypted is None else encrypted.decode("utf-8"),
        },
        dataset_snapshot={
            "id": dataset.id,
            "name": dataset.name,
            "config_name": config.name,
            "config_import": config.import_path,
            "dataset_symbol": config.symbol,
            "relative_data_path": entry.install_path,
        },
    )
    return _to_response(job, repository)


@router.get("/api/jobs", response_model=list[JobResponse])
def list_jobs(
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    job_status: str | None = Query(default=None, alias="status"),
    mode: str | None = Query(default=None),
) -> list[JobResponse]:
    jobs = repository.list_for_owner(user.id)
    if job_status is not None:
        jobs = [job for job in jobs if job.status == job_status]
    if mode is not None:
        jobs = [job for job in jobs if job.mode == mode]
    return [_to_response(job, repository) for job in jobs]


@router.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> JobResponse:
    job = repository.get_for_owner(job_id, user.id)
    if job is None:
        raise _not_found()
    return _to_response(job, repository)


@router.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> JobResponse:
    job = repository.get_for_owner(job_id, user.id)
    if job is None:
        raise _not_found()
    if job.status in {status_.value for status_ in TERMINAL_STATUSES}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"this job already finished as {job.status}",
        )
    stopped = repository.request_stop_for_owner(job_id, user.id)
    if stopped is None:
        raise _not_found()
    return _to_response(stopped, repository)


@router.get("/api/jobs/{job_id}/logs", response_model=LogChunk)
def read_logs(
    job_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    offset: int = Query(default=0, ge=0),
) -> LogChunk:
    """Return bytes after `offset` and the next offset, so a reconnect resumes exactly."""
    job = repository.get_for_owner(job_id, user.id)
    if job is None:
        raise _not_found()

    log_path = settings.jobs_dir / job.log_path
    if not log_path.is_file():
        return LogChunk(offset=offset if offset else 0, text="")
    with log_path.open("rb") as handle:
        handle.seek(min(offset, log_path.stat().st_size))
        chunk = handle.read(LOG_CHUNK_LIMIT)
        next_offset = handle.tell()
    return LogChunk(offset=next_offset, text=chunk.decode("utf-8", errors="replace"))


@router.websocket("/ws/jobs/{job_id}")
async def job_events(websocket: WebSocket, job_id: str) -> None:
    """Live notifications only. REST stays authoritative, so a reconnect loses nothing."""
    token = websocket.cookies.get(SESSION_COOKIE)
    user = None
    if token:
        repository = get_user_repository(websocket)
        user = repository.get_user_by_session_hash(session_token_digest(token))
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    jobs = JobRepository(websocket.app.state.database)
    if jobs.get_for_owner(job_id, user.id) is None:
        # Ownership is verified before accepting, so an unowned ID never opens a socket.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    notifier = websocket.app.state.notifier
    queue = notifier.subscribe(job_id)
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await queue.get())
    except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
        logger.debug("Job event stream for %s closed", job_id)
    finally:
        notifier.unsubscribe(job_id, queue)

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aisbench_web.datasets.catalog import CatalogEntry, load_catalog, load_model_configs
from aisbench_web.datasets.scan import DatasetConfig
from aisbench_web.dependencies import get_current_user, get_user_repository
from aisbench_web.jobs import dataset_progress
from aisbench_web.jobs.results import read_dataset_samples, safe_artifact_path
from aisbench_web.jobs.states import TERMINAL_STATUSES
from aisbench_web.repositories.datasets import Dataset, DatasetRepository, DatasetStatus
from aisbench_web.repositories.jobs import (
    Job,
    JobRepository,
    StoredDatasetProgress,
    dataset_entries,
)
from aisbench_web.repositories.models import ModelEndpointRepository
from aisbench_web.repositories.users import User
from aisbench_web.security import SESSION_COOKIE, session_token_digest
from aisbench_web.settings import Settings

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_CHUNK_LIMIT = 256 * 1024
JOB_NOT_FOUND = "job not found"
JOB_NAME_MAX_LENGTH = 200
MAX_DATASETS_PER_JOB = 16
ACCURACY = "accuracy"
PERFORMANCE = "performance"


class CommonCliOptions(BaseModel):
    """Arguments AISBench reads from its own command line, in every mode.

    These are a different thing from the model config's fields: the command line drives the
    run, the config file describes the endpoint. Mixing them is what made the old form
    offer settings that never reached anything.
    """

    model_config = ConfigDict(extra="forbid")

    num_prompts: int | None = Field(default=None, ge=1, le=1_000_000)
    max_num_workers: int = Field(default=1, ge=1, le=128)
    max_workers_per_gpu: int | None = Field(default=None, ge=1, le=128)
    num_warmups: int | None = Field(default=None, ge=0, le=1000)


class AccuracyCliOptions(CommonCliOptions):
    dump_eval_details: bool = False
    merge_datasets: bool = False
    dump_extract_rate: bool = False


class PerformanceCliOptions(CommonCliOptions):
    pressure: bool = False
    pressure_time: int | None = Field(default=None, ge=1, le=86400)
    spec_decode: bool = False


CLI_OPTION_MODELS = {ACCURACY: AccuracyCliOptions, PERFORMANCE: PerformanceCliOptions}

#: A model config holds plain values; anything else is not something the file declares.
FieldValue = bool | int | float | str


class JobParameters(BaseModel):
    """What the user filled in, kept in the two groups AISBench actually has."""

    model_config = ConfigDict(extra="forbid")

    #: Overrides for fields of the chosen model config file, by that file's own names.
    config_fields: dict[str, FieldValue] = Field(default_factory=dict)
    #: Overrides for entries of that file's generation_kwargs.
    generation_kwargs: dict[str, FieldValue] = Field(default_factory=dict)
    cli: dict[str, Any] = Field(default_factory=dict)


class JobCreate(BaseModel):
    name: str = Field(default="", max_length=JOB_NAME_MAX_LENGTH)
    model_endpoint_id: str
    dataset_ids: list[str] = Field(min_length=1, max_length=MAX_DATASETS_PER_JOB)
    mode: Literal["accuracy", "performance"]
    #: A specific AISBench config per dataset id; the mode's default when a dataset is absent.
    config_names: dict[str, str] = Field(default_factory=dict)
    #: Which AISBench model config drives the endpoint; the mode's default when omitted.
    model_config_name: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


def _reject_fields_the_config_does_not_have(model_config, parameters: dict) -> None:
    """A field the chosen file does not declare would be written into the config regardless,
    where AISBench would either ignore it or fail. Name it here instead."""
    if model_config is None:
        return
    for key, declared in (
        ("config_fields", {field.name for field in model_config.fields}),
        ("generation_kwargs", {field.name for field in model_config.generation_fields}),
    ):
        unknown = sorted(set(parameters.get(key) or {}) - declared)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"model config {model_config.name!r} has no "
                    f"{'generation_kwargs entry' if key == 'generation_kwargs' else 'field'} "
                    f"named {unknown[0]!r}"
                ),
            )


class ModelDisplay(BaseModel):
    name: str
    model_name: str
    base_url: str
    config_name: str = ""


class DatasetDisplay(BaseModel):
    id: str
    name: str
    config_name: str = ""


class DatasetMetricDisplay(BaseModel):
    value: float | None
    text_value: str | None
    unit: str | None


class DatasetProgressDisplay(BaseModel):
    name: str
    phase: str
    completed: int | None = None
    total: int | None = None
    rate: str | None = None
    counters: dict[str, Any] | None = None
    log_available: bool = False
    metrics: dict[str, DatasetMetricDisplay] = Field(default_factory=dict)
    correct_count: int | None = None
    total_count: int | None = None
    started_at: str | None = None


class JobProgress(BaseModel):
    completed: int
    total: int


class JobResponse(BaseModel):
    id: str
    name: str
    mode: str
    status: str
    queue_position: int | None
    progress: JobProgress | None
    model: ModelDisplay
    dataset: DatasetDisplay
    datasets: list[DatasetProgressDisplay] = Field(default_factory=list)
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


def _to_response(
    job: Job, repository: JobRepository, *, include_datasets: bool = False
) -> JobResponse:
    model = job.model_snapshot
    entries = dataset_entries(job.dataset_snapshot)
    first = entries[0] if entries else {}
    ahead = repository.queue_position(job.id)
    return JobResponse(
        id=job.id,
        name=job.name,
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
            config_name=model.get("config_name", ""),
        ),
        dataset=DatasetDisplay(
            id=first.get("id", ""),
            name=first.get("name", ""),
            config_name=first.get("config_name", ""),
        ),
        datasets=_dataset_rows(job, repository) if include_datasets else [],
        parameters=job.parameters,
        exit_code=job.exit_code,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _dataset_rows(job: Job, repository: JobRepository) -> list[DatasetProgressDisplay]:
    """Progress rows ordered the way the job was configured, with anything AISBench reported
    under another name (a merged run) appended after them."""
    stored = {row.dataset: row for row in repository.list_dataset_progress(job.id)}
    ordered: list[DatasetProgressDisplay] = []
    seen: set[str] = set()
    for entry in dataset_entries(job.dataset_snapshot):
        # AISBench reports datasets under their abbr, and so do the progress rows.
        for key in (entry.get("abbr"), entry.get("config_name"), entry.get("name")):
            row = stored.get(key or "")
            if row is not None:
                seen.add(row.dataset)
                ordered.append(_dataset_display(row))
                break
    return ordered + [
        _dataset_display(row) for name, row in stored.items() if name not in seen
    ]


def _dataset_display(row) -> DatasetProgressDisplay:
    metrics = row.metrics if isinstance(row.metrics, dict) else {}
    return DatasetProgressDisplay(
        name=row.dataset,
        phase=row.phase,
        completed=row.completed,
        total=row.total,
        rate=row.rate,
        counters=row.counters,
        log_available=bool(row.log_path),
        metrics={
            name: DatasetMetricDisplay(
                value=entry.get("value") if isinstance(entry, dict) else None,
                text_value=entry.get("text_value") if isinstance(entry, dict) else None,
                unit=entry.get("unit") if isinstance(entry, dict) else None,
            )
            for name, entry in metrics.items()
        },
        correct_count=row.correct_count,
        total_count=row.total_count,
        started_at=row.started_at,
    )


@router.post("/api/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreate,
    request: Request,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> JobResponse:
    """Ownership comes from the session; the request body cannot name an owner."""
    try:
        requested = JobParameters(**payload.parameters)
        parameters = requested.model_dump()
        parameters["cli"] = CLI_OPTION_MODELS[payload.mode](**requested.cli).model_dump()
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

    datasets = DatasetRepository(request.app.state.database)
    catalog = load_catalog()
    unknown_config_keys = set(payload.config_names) - set(payload.dataset_ids)
    if unknown_config_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"config_names names datasets the job does not include: "
                f"{min(unknown_config_keys)!r}"
            ),
        )

    selected: list[tuple[Dataset, CatalogEntry, DatasetConfig]] = []
    seen_imports: set[str] = set()
    for dataset_id in dict.fromkeys(payload.dataset_ids):
        dataset = datasets.get(dataset_id)
        if dataset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"dataset {dataset_id!r} not found",
            )
        if dataset.status != DatasetStatus.AVAILABLE.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"dataset {dataset.name!r} is not installed yet; "
                    f"install it before submitting a job"
                ),
            )
        entry = next((item for item in catalog if item.id == dataset_id), None)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"dataset {dataset_id!r} not found",
            )
        requested_name = payload.config_names.get(dataset_id)
        config = (
            entry.config_named(requested_name)
            if requested_name is not None
            else entry.default_config(payload.mode)
        )
        if config is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"dataset {dataset.name!r} has no {payload.mode} configuration "
                    f"in the installed AISBench"
                ),
            )
        if config.mode != payload.mode:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"configuration {config.name!r} is a {config.mode} configuration",
            )
        if config.import_path in seen_imports:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"configuration {config.name!r} is already part of this job; "
                    f"it would run the same dataset twice"
                ),
            )
        seen_imports.add(config.import_path)
        selected.append((dataset, entry, config))

    model_config = None
    if payload.model_config_name is not None:
        model_config = next(
            (
                candidate
                for candidate in load_model_configs()
                if candidate.name == payload.model_config_name
            ),
            None,
        )
        if model_config is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"the installed AISBench has no model config named "
                    f"{payload.model_config_name!r}"
                ),
            )

    _reject_fields_the_config_does_not_have(model_config, parameters)

    encrypted = endpoints.get_encrypted_api_key_for_owner(user.id, endpoint.id)
    first_dataset, _first_entry, first_config = selected[0]
    job = repository.create(
        owner_id=user.id,
        # AISBench names a config after its dataset, so pairing the two reads "ARC_c ·
        # ARC_c_gen_0_shot_chat_prompt". The config name alone already says both.
        name=payload.name.strip()
        or (
            first_config.name
            if len(selected) == 1
            else f"{first_dataset.name} +{len(selected) - 1}"
        ),
        model_endpoint_id=endpoint.id,
        # The column carries the first dataset; the snapshot carries all of them.
        dataset_id=first_dataset.id,
        mode=payload.mode,
        parameters=parameters,
        # Snapshots are display-stable: renaming the endpoint later cannot rewrite history.
        model_snapshot={
            "abbr": endpoint.name,
            "name": endpoint.name,
            "base_url": endpoint.base_url,
            "model_name": endpoint.model_name,
            "config_name": "" if model_config is None else model_config.name,
            "config_import": "" if model_config is None else model_config.import_path,
            "encrypted_api_key": None if encrypted is None else encrypted.decode("utf-8"),
        },
        dataset_snapshot={
            "datasets": [
                {
                    "id": dataset.id,
                    "name": dataset.name,
                    "config_name": config.name,
                    "config_import": config.import_path,
                    "dataset_symbol": config.symbol,
                    "abbr": config.abbr,
                    "relative_data_path": entry.install_path,
                }
                for dataset, entry, config in selected
            ]
        },
    )
    # The rows exist from the moment the job does, so a page opened straight after
    # submitting shows every dataset as queued instead of an empty column.
    repository.replace_dataset_progress(
        job.id,
        [
            dataset_progress.DatasetStatus(
                dataset=config.abbr, phase=dataset_progress.PHASE_QUEUED
            )
            for _, _, config in selected
            if config.abbr
        ],
    )
    return _to_response(job, repository, include_datasets=True)


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
    return _to_response(job, repository, include_datasets=True)


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
    return _read_log_chunk(settings.jobs_dir / job.log_path, offset)


@router.get("/api/jobs/{job_id}/datasets/{dataset}/logs", response_model=LogChunk)
def read_dataset_logs(
    job_id: str,
    dataset: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    offset: int = Query(default=0, ge=0),
) -> LogChunk:
    """The log of one dataset's own task, addressed by the name progress reports it under.

    The name is matched against the stored progress rows rather than turned into a path, so
    it can never address a file outside the job's directory however it is spelled.
    """
    job, row = _owned_dataset_row(job_id, dataset, user, repository)
    if not row.log_path:
        return LogChunk(offset=offset if offset else 0, text="")
    try:
        log_path = safe_artifact_path(settings.jobs_dir / job.id, row.log_path)
    except ValueError:
        return LogChunk(offset=offset if offset else 0, text="")
    return _read_log_chunk(log_path, offset)


class SampleDisplay(BaseModel):
    id: str
    prompt: str | None
    origin_prediction: str | None
    prediction: str | None
    reference: str | None
    correct: bool | None


class SamplesResponse(BaseModel):
    #: Which of the run's files the preview was read from; "none" when there is nothing.
    source: str
    total: int
    samples: list[SampleDisplay]


@router.get("/api/jobs/{job_id}/datasets/{dataset}/samples", response_model=SamplesResponse)
def list_dataset_samples(
    job_id: str,
    dataset: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    settings: SettingsDependency,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> SamplesResponse:
    """A page of per-sample records: what the model was asked, answered, and whether it
    was right. The evaluator's details when the run dumped them, the predictions file
    otherwise."""
    job, _ = _owned_dataset_row(job_id, dataset, user, repository)
    read = read_dataset_samples(
        settings.jobs_dir / job.output_dir, str(job.model_snapshot.get("abbr") or ""), dataset
    )
    return SamplesResponse(
        source=read.source,
        total=read.total,
        samples=[
            SampleDisplay(
                id=sample.id,
                prompt=sample.prompt,
                origin_prediction=sample.origin_prediction,
                prediction=sample.prediction,
                reference=sample.reference,
                correct=sample.correct,
            )
            for sample in read.samples[offset : offset + limit]
        ],
    )


def _owned_dataset_row(
    job_id: str, dataset: str, user: User, repository: JobRepository
) -> tuple[Job, StoredDatasetProgress]:
    job = repository.get_for_owner(job_id, user.id)
    if job is None:
        raise _not_found()
    row = repository.get_dataset_progress(job_id, dataset)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this job reports no dataset under that name",
        )
    return job, row


def _read_log_chunk(log_path: Path, offset: int) -> LogChunk:
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

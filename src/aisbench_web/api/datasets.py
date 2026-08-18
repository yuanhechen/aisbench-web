import logging
from concurrent.futures import Executor
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from aisbench_web.datasets.catalog import CatalogEntry, load_catalog, resolve_datasets_root
from aisbench_web.datasets.installer import DatasetInstaller
from aisbench_web.db import Database
from aisbench_web.dependencies import get_current_user
from aisbench_web.repositories.datasets import Dataset, DatasetRepository
from aisbench_web.repositories.users import User
from aisbench_web.settings import Settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/datasets")


class DatasetConfigResponse(BaseModel):
    """One AISBench config file: a specific way of running this dataset.

    `name` is the identity the CLI uses; the rest is read off it for display only.
    """

    name: str
    mode: str
    method: str
    shots: int | None
    chain_of_thought: bool
    chat_prompt: bool
    alias_of: str


class DatasetResponse(BaseModel):
    id: str
    name: str
    description: str
    config_name: str
    category: str
    task: str
    configs: list[DatasetConfigResponse]
    status: str
    local_path: str | None
    size_bytes: int | None
    error_message: str | None
    can_install: bool

    @classmethod
    def from_dataset(cls, dataset: Dataset, entry: CatalogEntry | None) -> "DatasetResponse":
        return cls(
            id=dataset.id,
            name=dataset.name,
            description=dataset.description,
            config_name=dataset.config_name,
            category=dataset.category,
            task=dataset.task,
            configs=[
                DatasetConfigResponse(
                    name=config.name,
                    mode=config.mode,
                    method=config.method,
                    shots=config.shots,
                    chain_of_thought=config.chain_of_thought,
                    chat_prompt=config.chat_prompt,
                    alias_of=config.alias_of,
                )
                for config in (() if entry is None else entry.configs)
            ],
            status=dataset.status,
            local_path=dataset.local_path,
            size_bytes=dataset.size_bytes,
            error_message=dataset.error_message,
            can_install=dataset.can_install,
        )


def get_dataset_repository(request: Request) -> DatasetRepository:
    return DatasetRepository(request.app.state.database)


RepositoryDependency = Annotated[DatasetRepository, Depends(get_dataset_repository)]
CurrentUserDependency = Annotated[User, Depends(get_current_user)]


def _catalog_by_id() -> dict[str, CatalogEntry]:
    return {entry.id: entry for entry in load_catalog()}


def run_install(
    database: Database,
    settings: Settings,
    entry: CatalogEntry,
    transport: httpx.BaseTransport | None,
) -> None:
    """Blocking installer body; the shared row is the only progress channel."""
    repository = DatasetRepository(database)
    try:
        root = resolve_datasets_root()
        if root is None:
            raise ValueError(
                "Could not locate the AISBench dataset directory; set AISBENCH_DATASETS_DIR"
            )
        installer = DatasetInstaller(settings.downloads_dir, transport=transport)
        target = installer.install(entry, root / entry.install_path)
    except Exception as exc:
        # Any failure is reported through the dataset row, never by crashing the worker thread.
        logger.warning("Installing dataset %s failed", entry.id, exc_info=exc)
        repository.mark_failed(entry.id, str(exc) or exc.__class__.__name__)
        return
    size = None if entry.download is None else entry.download.size_bytes
    repository.mark_available(entry.id, local_path=str(target), size_bytes=size)


@router.get("", response_model=list[DatasetResponse])
def list_datasets(
    _user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> list[DatasetResponse]:
    catalog = _catalog_by_id()
    return [
        DatasetResponse.from_dataset(dataset, catalog.get(dataset.id))
        for dataset in repository.list_all()
    ]


@router.get("/{dataset_id}", response_model=DatasetResponse)
def get_dataset(
    dataset_id: str,
    _user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> DatasetResponse:
    dataset = repository.get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dataset not found")
    return DatasetResponse.from_dataset(dataset, _catalog_by_id().get(dataset_id))


@router.post(
    "/{dataset_id}/install",
    response_model=DatasetResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def install_dataset(
    dataset_id: str,
    request: Request,
    response: Response,
    _user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> DatasetResponse:
    dataset = repository.get(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dataset not found")

    entry = _catalog_by_id().get(dataset_id)
    if entry is None or not entry.can_install:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this dataset has no verified source, so one-click install is unavailable",
        )

    if repository.acquire_install_lock(dataset_id):
        _submit_install(request, entry)
    # A losing caller still sees `installing`: the winner owns the shared slot.
    current = repository.get(dataset_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dataset not found")
    response.status_code = status.HTTP_202_ACCEPTED
    return DatasetResponse.from_dataset(current, entry)


def _submit_install(request: Request, entry: CatalogEntry) -> None:
    executor: Executor = request.app.state.install_executor
    tasks = request.app.state.install_tasks
    tasks[:] = [task for task in tasks if not task.done()]
    tasks.append(
        executor.submit(
            run_install,
            request.app.state.database,
            request.app.state.settings,
            entry,
            getattr(request.app.state, "http_transport", None),
        )
    )

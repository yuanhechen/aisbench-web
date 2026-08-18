import importlib.util
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path

from aisbench_web.datasets.scan import DatasetConfig, ScannedDataset, scan_dataset_configs
from aisbench_web.db import Database
from aisbench_web.repositories.datasets import DatasetRepository, DatasetStatus
from aisbench_web.settings import Settings

logger = logging.getLogger(__name__)

DOWNLOADS_RESOURCE = "downloads.json"
CATEGORIES_RESOURCE = "categories.json"
UNCATEGORISED = "other"
STALE_PART_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class DownloadSource:
    url: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class CatalogEntry:
    """A dataset the installed AISBench supports, plus how to obtain it."""

    id: str
    install_path: str | None
    required_path: str | None
    configs: tuple[DatasetConfig, ...]
    download: DownloadSource | None
    #: Domain from the AISBench documentation, or "other" when it lists none.
    category: str = UNCATEGORISED
    #: Task type the documentation prints for this dataset, empty when it lists none.
    task: str = ""

    @property
    def can_install(self) -> bool:
        return self.download is not None

    def configs_for(self, mode: str) -> tuple[DatasetConfig, ...]:
        return tuple(config for config in self.configs if config.mode == mode)

    def default_config(self, mode: str) -> DatasetConfig | None:
        candidates = self.configs_for(mode)
        return candidates[0] if candidates else None

    def config_named(self, name: str) -> DatasetConfig | None:
        return next((config for config in self.configs if config.name == name), None)


@lru_cache(maxsize=1)
def load_download_sources() -> dict[str, DownloadSource]:
    """Verified archives, keyed by the directory they unpack into.

    Each entry was downloaded and checked against the path the installed configs expect;
    an archive that unpacks somewhere else is not listed, because installing it would put
    the data where nothing looks for it.
    """
    package = resources.files("aisbench_web.datasets")
    raw = json.loads(package.joinpath(DOWNLOADS_RESOURCE).read_text(encoding="utf-8"))
    return {
        directory: DownloadSource(
            url=source["url"], sha256=source["sha256"], size_bytes=source["size_bytes"]
        )
        for directory, source in raw["downloads"].items()
    }


@lru_cache(maxsize=1)
def load_tasks() -> dict[str, str]:
    """Map a dataset directory to the task type the documentation prints for it."""
    package = resources.files("aisbench_web.datasets")
    raw = json.loads(package.joinpath(CATEGORIES_RESOURCE).read_text(encoding="utf-8"))
    tasks = {name.casefold(): task for name, task in raw.get("tasks", {}).items()}
    for directory, documented_name in raw.get("aliases", {}).items():
        task = tasks.get(documented_name.casefold())
        if task is not None:
            tasks[directory.casefold()] = task
    return tasks


@lru_cache(maxsize=1)
def load_categories() -> dict[str, str]:
    """Map a dataset directory to the domain the AISBench documentation puts it in.

    The documentation is the only place these domains are published; nothing in the installed
    package records them. A dataset the documentation does not list stays uncategorised rather
    than being guessed into a domain.
    """
    package = resources.files("aisbench_web.datasets")
    raw = json.loads(package.joinpath(CATEGORIES_RESOURCE).read_text(encoding="utf-8"))
    documented = {
        name.casefold(): category
        for category, names in raw["categories"].items()
        for name in names
    }
    # A directory whose name differs from the one the documentation prints.
    for directory, documented_name in raw.get("aliases", {}).items():
        category = documented.get(documented_name.casefold())
        if category is not None:
            documented[directory.casefold()] = category
    return documented


def resolve_ais_bench_package() -> Path | None:
    """Locate the installed ais_bench package, or None when it is not importable."""
    try:
        spec = importlib.util.find_spec("ais_bench")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))).resolve()


def resolve_datasets_root() -> Path | None:
    """Locate AISBench's dataset directory.

    AISBench dataset configs declare ``path='ais_bench/datasets/<name>'`` relative to the
    source root, so the directory to populate is ``<ais_bench package>/datasets``.
    """
    override = os.environ.get("AISBENCH_DATASETS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    package = resolve_ais_bench_package()
    return None if package is None else package / "datasets"


def load_catalog() -> tuple[CatalogEntry, ...]:
    """Read the catalog from the installed AISBench, pairing it with verified download sources."""
    package = resolve_ais_bench_package()
    override = os.environ.get("AISBENCH_CONFIGS_PACKAGE")
    if override:
        package = Path(override).expanduser().resolve()
    if package is None:
        return ()
    return _entries_for(scan_dataset_configs(package))


def _entries_for(scanned: tuple[ScannedDataset, ...]) -> tuple[CatalogEntry, ...]:
    sources = load_download_sources()
    categories = load_categories()
    tasks = load_tasks()
    return tuple(
        CatalogEntry(
            id=dataset.id,
            install_path=dataset.install_path,
            required_path=dataset.required_path,
            configs=dataset.configs,
            download=sources.get(dataset.install_path or ""),
            category=categories.get(dataset.id.casefold(), UNCATEGORISED),
            task=tasks.get(dataset.id.casefold(), ""),
        )
        for dataset in scanned
    )


class CatalogService:
    """Reconcile what AISBench supports and what is on disk with the shared dataset rows."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.repository = DatasetRepository(database)
        self.settings = settings

    def sync(self) -> None:
        root = resolve_datasets_root()
        entries = load_catalog()
        if not entries:
            logger.warning(
                "Could not read dataset configs from AISBench; "
                "the shared dataset catalog will be empty"
            )

        # A row left in `installing` belongs to a process that is no longer running.
        self.repository.release_stale_install_locks()
        self._remove_stale_part_files()

        timestamp = datetime.now(timezone.utc).isoformat()
        for entry in entries:
            installed = self._installed_path(root, entry)
            self.repository.upsert_catalog_entry(
                entry_id=entry.id,
                config_name=self._display_config(entry),
                name=entry.id,
                description="",
                download_url=None if entry.download is None else entry.download.url,
                sha256=None if entry.download is None else entry.download.sha256,
                size_bytes=None if entry.download is None else entry.download.size_bytes,
                local_path=None if installed is None else str(installed),
                status=DatasetStatus.AVAILABLE if installed else DatasetStatus.NOT_INSTALLED,
                category=entry.category,
                task=entry.task,
                updated_at=timestamp,
            )
        self.repository.forget_datasets_other_than([entry.id for entry in entries])

    @staticmethod
    def _installed_path(root: Path | None, entry: CatalogEntry) -> Path | None:
        """A dataset counts as installed only when the path its configs read actually exists."""
        if root is None or entry.required_path is None:
            return None
        required = root / entry.required_path
        if not required.exists():
            return None
        return root / (entry.install_path or entry.required_path)

    @staticmethod
    def _display_config(entry: CatalogEntry) -> str:
        default = entry.default_config("accuracy") or entry.default_config("performance")
        return entry.id if default is None else default.name

    def _remove_stale_part_files(self) -> None:
        downloads = self.settings.downloads_dir
        if not downloads.is_dir():
            return
        cutoff = time.time() - STALE_PART_AGE_SECONDS
        for part in downloads.glob("*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                logger.warning("Could not remove stale download %s", part)

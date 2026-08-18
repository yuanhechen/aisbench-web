import importlib.util
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path

from aisbench_web.db import Database
from aisbench_web.repositories.datasets import DatasetRepository, DatasetStatus
from aisbench_web.settings import Settings

logger = logging.getLogger(__name__)

CATALOG_RESOURCE = "catalog.json"
STALE_PART_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    description: str
    accuracy_config: str
    performance_config: str | None
    relative_data_path: str
    download_url: str | None
    sha256: str | None
    size_bytes: int | None

    @property
    def can_install(self) -> bool:
        return self.download_url is not None

    def replace(self, **changes: object) -> "CatalogEntry":
        return replace(self, **changes)


def _validated_entry(raw: dict) -> CatalogEntry:
    relative = Path(raw["relative_data_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"catalog entry {raw['id']!r} has an unsafe relative_data_path")
    return CatalogEntry(
        id=raw["id"],
        name=raw["name"],
        description=raw["description"],
        accuracy_config=raw["accuracy_config"],
        performance_config=raw.get("performance_config"),
        relative_data_path=raw["relative_data_path"],
        download_url=raw.get("download_url"),
        sha256=raw.get("sha256"),
        size_bytes=raw.get("size_bytes"),
    )


@lru_cache(maxsize=1)
def load_catalog() -> tuple[CatalogEntry, ...]:
    """Load the packaged, trusted dataset manifest."""
    package = resources.files("aisbench_web.datasets")
    raw = json.loads(package.joinpath(CATALOG_RESOURCE).read_text(encoding="utf-8"))
    return tuple(_validated_entry(entry) for entry in raw["datasets"])


def resolve_datasets_root() -> Path | None:
    """Locate AISBench's dataset directory, or None when AISBench is not importable.

    AISBench dataset configs declare ``path='ais_bench/datasets/<name>'`` relative to the source
    root, so the directory to populate is ``<ais_bench package>/datasets``.
    """
    override = os.environ.get("AISBENCH_DATASETS_DIR")
    if override:
        return Path(override).expanduser().resolve()

    try:
        spec = importlib.util.find_spec("ais_bench")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))).resolve() / "datasets"


class CatalogService:
    """Reconcile the packaged manifest and the on-disk layout with the shared dataset rows."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.repository = DatasetRepository(database)
        self.settings = settings

    def sync(self) -> None:
        root = resolve_datasets_root()
        if root is None:
            logger.warning(
                "Could not locate the AISBench dataset directory; "
                "set AISBENCH_DATASETS_DIR to enable dataset installation"
            )

        # A row left in `installing` belongs to a process that is no longer running, so its lock
        # must be released before anyone can retry.
        self.repository.release_stale_install_locks()
        self._remove_stale_part_files()

        timestamp = datetime.now(timezone.utc).isoformat()
        catalog_paths: set[Path] = set()
        for entry in load_catalog():
            local_path = None if root is None else root / entry.relative_data_path
            if local_path is not None:
                catalog_paths.add(local_path)
            installed = local_path is not None and local_path.is_dir()
            self.repository.upsert_catalog_entry(
                entry_id=entry.id,
                config_name=entry.accuracy_config,
                name=entry.name,
                description=entry.description,
                download_url=entry.download_url,
                sha256=entry.sha256,
                size_bytes=entry.size_bytes,
                local_path=str(local_path) if installed else None,
                status=DatasetStatus.AVAILABLE if installed else DatasetStatus.NOT_INSTALLED,
                updated_at=timestamp,
            )

        if root is not None:
            self._record_detected_directories(root, catalog_paths, timestamp)

    def _record_detected_directories(
        self,
        root: Path,
        catalog_paths: set[Path],
        timestamp: str,
    ) -> None:
        """Surface datasets already present in AISBench that the manifest does not cover."""
        if not root.is_dir():
            return
        for candidate in sorted(root.iterdir()):
            if candidate in catalog_paths or candidate.name.startswith("."):
                continue
            if not candidate.is_dir():
                continue
            self.repository.upsert_detected_directory(
                entry_id=candidate.name,
                name=candidate.name,
                local_path=str(candidate),
                updated_at=timestamp,
            )

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

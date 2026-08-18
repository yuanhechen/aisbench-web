from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from aisbench_web.db import Database


class DatasetStatus(str, Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    AVAILABLE = "available"
    FAILED = "failed"
    DETECTED = "detected"


SELECTED_COLUMNS = (
    "id, config_name, name, description, status, local_path, download_url, "
    "size_bytes, error_message, category"
)


@dataclass(frozen=True)
class Dataset:
    id: str
    config_name: str
    name: str
    description: str
    status: str
    local_path: str | None
    download_url: str | None
    size_bytes: int | None
    error_message: str | None
    category: str

    @property
    def can_install(self) -> bool:
        return self.download_url is not None


class DatasetRepository:
    """Datasets are shared by every user, so nothing here filters by owner."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_catalog_entry(
        self,
        *,
        entry_id: str,
        config_name: str,
        name: str,
        description: str,
        download_url: str | None,
        sha256: str | None,
        size_bytes: int | None,
        local_path: str | None,
        status: DatasetStatus,
        category: str,
        updated_at: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO datasets (
                  id, config_name, name, description, status, local_path,
                  download_url, sha256, size_bytes, error_message, category,
                  installed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  config_name = excluded.config_name,
                  name = excluded.name,
                  description = excluded.description,
                  status = excluded.status,
                  local_path = excluded.local_path,
                  download_url = excluded.download_url,
                  sha256 = excluded.sha256,
                  size_bytes = excluded.size_bytes,
                  error_message = NULL,
                  category = excluded.category,
                  installed_at = excluded.installed_at,
                  updated_at = excluded.updated_at
                """,
                (
                    entry_id,
                    config_name,
                    name,
                    description,
                    status.value,
                    local_path,
                    download_url,
                    sha256,
                    size_bytes,
                    category,
                    updated_at if local_path else None,
                    updated_at,
                ),
            )

    def upsert_detected_directory(
        self,
        *,
        entry_id: str,
        name: str,
        local_path: str,
        updated_at: str,
    ) -> None:
        """Record a dataset present in AISBench that the packaged manifest does not describe."""
        self.upsert_catalog_entry(
            entry_id=entry_id,
            config_name=entry_id,
            name=name,
            description="Detected in the AISBench environment; one-click install is unavailable.",
            download_url=None,
            sha256=None,
            size_bytes=None,
            local_path=local_path,
            status=DatasetStatus.DETECTED,
            category="other",
            updated_at=updated_at,
        )

    def forget_datasets_other_than(self, known_ids: list[str]) -> None:
        """Drop rows for datasets the installed AISBench no longer ships a config for."""
        if not known_ids:
            return
        placeholders = ", ".join("?" for _ in known_ids)
        with self.database.connect() as connection:
            connection.execute(
                f"DELETE FROM datasets WHERE id NOT IN ({placeholders})",
                known_ids,
            )

    def list_all(self) -> list[Dataset]:
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {SELECTED_COLUMNS} FROM datasets ORDER BY id"
            ).fetchall()
        return [Dataset(**dict(row)) for row in rows]

    def get(self, dataset_id: str) -> Dataset | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {SELECTED_COLUMNS} FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()
        return None if row is None else Dataset(**dict(row))

    def acquire_install_lock(self, dataset_id: str) -> bool:
        """Claim the shared install slot for one dataset; a single conditional UPDATE is atomic."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE datasets
                SET status = ?, error_message = NULL, updated_at = ?
                WHERE id = ? AND status != ?
                """,
                (
                    DatasetStatus.INSTALLING.value,
                    self._now(),
                    dataset_id,
                    DatasetStatus.INSTALLING.value,
                ),
            )
        return cursor.rowcount == 1

    def release_stale_install_locks(self) -> None:
        """An `installing` row at startup belongs to a process that is gone."""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE datasets
                SET status = ?, updated_at = ?
                WHERE status = ?
                """,
                (DatasetStatus.NOT_INSTALLED.value, self._now(), DatasetStatus.INSTALLING.value),
            )

    def mark_available(self, dataset_id: str, *, local_path: str, size_bytes: int | None) -> None:
        timestamp = self._now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE datasets
                SET status = ?, local_path = ?, size_bytes = COALESCE(?, size_bytes),
                    error_message = NULL, installed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    DatasetStatus.AVAILABLE.value,
                    local_path,
                    size_bytes,
                    timestamp,
                    timestamp,
                    dataset_id,
                ),
            )

    def mark_failed(self, dataset_id: str, message: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE datasets
                SET status = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (DatasetStatus.FAILED.value, message, self._now(), dataset_id),
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

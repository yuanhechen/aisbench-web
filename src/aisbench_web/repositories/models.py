import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from aisbench_web.db import Database

UPDATABLE_COLUMNS = ("name", "base_url", "model_name")
SELECTED_COLUMNS = (
    "id, name, base_url, model_name, encrypted_api_key IS NOT NULL AS has_api_key, is_active"
)


class DuplicateEndpointNameError(Exception):
    """Raised when an owner already has a model endpoint with the requested name."""


@dataclass(frozen=True)
class ModelEndpoint:
    id: str
    name: str
    base_url: str
    model_name: str
    has_api_key: bool
    is_active: bool


class ModelEndpointRepository:
    """Owner-scoped persistence: every statement filters on ``owner_id``."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        owner_id: str,
        name: str,
        base_url: str,
        model_name: str,
        encrypted_api_key: bytes | None,
    ) -> ModelEndpoint:
        endpoint_id = str(uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO model_endpoints (
                      id, owner_id, name, base_url, model_name, encrypted_api_key,
                      is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        endpoint_id,
                        owner_id,
                        name,
                        base_url,
                        model_name,
                        encrypted_api_key,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "model_endpoints.owner_id" not in str(exc):
                    raise
                raise DuplicateEndpointNameError(name) from exc
        return ModelEndpoint(
            id=endpoint_id,
            name=name,
            base_url=base_url,
            model_name=model_name,
            has_api_key=encrypted_api_key is not None,
            is_active=True,
        )

    def list_for_owner(self, owner_id: str) -> list[ModelEndpoint]:
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {SELECTED_COLUMNS}
                FROM model_endpoints
                WHERE owner_id = ?
                ORDER BY created_at, id
                """,
                (owner_id,),
            ).fetchall()
        return [self._endpoint_from_row(row) for row in rows]

    def get_for_owner(self, owner_id: str, endpoint_id: str) -> ModelEndpoint | None:
        row = self._row_for_owner(owner_id, endpoint_id, SELECTED_COLUMNS)
        return None if row is None else self._endpoint_from_row(row)

    def get_encrypted_api_key_for_owner(self, owner_id: str, endpoint_id: str) -> bytes | None:
        row = self._row_for_owner(owner_id, endpoint_id, "encrypted_api_key")
        return None if row is None else row["encrypted_api_key"]

    def update_for_owner(
        self,
        owner_id: str,
        endpoint_id: str,
        *,
        changes: dict[str, object],
        api_key_replacement: bytes | None = None,
        replace_api_key: bool = False,
    ) -> ModelEndpoint | None:
        assignments = [f"{column} = ?" for column in changes if column in UPDATABLE_COLUMNS]
        values: list[object] = [
            value for column, value in changes.items() if column in UPDATABLE_COLUMNS
        ]
        if "is_active" in changes:
            assignments.append("is_active = ?")
            values.append(1 if changes["is_active"] else 0)
        if replace_api_key:
            assignments.append("encrypted_api_key = ?")
            values.append(api_key_replacement)
        assignments.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())

        with self.database.connect() as connection:
            try:
                cursor = connection.execute(
                    f"""
                    UPDATE model_endpoints
                    SET {", ".join(assignments)}
                    WHERE owner_id = ? AND id = ?
                    """,
                    (*values, owner_id, endpoint_id),
                )
            except sqlite3.IntegrityError as exc:
                if "model_endpoints.owner_id" not in str(exc):
                    raise
                raise DuplicateEndpointNameError(str(changes.get("name"))) from exc
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                f"""
                SELECT {SELECTED_COLUMNS}
                FROM model_endpoints
                WHERE owner_id = ? AND id = ?
                """,
                (owner_id, endpoint_id),
            ).fetchone()
        return None if row is None else self._endpoint_from_row(row)

    def _row_for_owner(self, owner_id: str, endpoint_id: str, columns: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute(
                f"""
                SELECT {columns}
                FROM model_endpoints
                WHERE owner_id = ? AND id = ?
                """,
                (owner_id, endpoint_id),
            ).fetchone()

    @staticmethod
    def _endpoint_from_row(row: sqlite3.Row) -> ModelEndpoint:
        return ModelEndpoint(
            id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            model_name=row["model_name"],
            has_api_key=bool(row["has_api_key"]),
            is_active=bool(row["is_active"]),
        )

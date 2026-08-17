import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aisbench_web.app import create_app
from aisbench_web.db import Database
from aisbench_web.settings import Settings


def test_concurrent_first_time_migrations_retry_locked_wal(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    wal_barrier = threading.Barrier(2)
    failure_guard = threading.Lock()
    connections_guard = threading.Lock()
    created_connections = []
    failure_injected = False

    class LockOnceDuringWalConnection(sqlite3.Connection):
        wal_attempts = 0
        was_closed = False

        def execute(self, sql, parameters=(), /):
            nonlocal failure_injected
            if sql == "PRAGMA journal_mode=WAL":
                self.wal_attempts += 1
                if self.wal_attempts == 1:
                    wal_barrier.wait(timeout=2)
                    with failure_guard:
                        if not failure_injected:
                            failure_injected = True
                            raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, parameters)

        def close(self):
            self.was_closed = True
            super().close()

    def synchronized_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=LockOnceDuringWalConnection)
        with connections_guard:
            created_connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", synchronized_connect)
    database_path = tmp_path / "app.db"
    databases = [Database(database_path), Database(database_path)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(database.migrate) for database in databases]
        for future in futures:
            future.result(timeout=5)

    assert failure_injected
    with Database(database_path).connect() as connection:
        versions = connection.execute(
            "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version"
        ).fetchall()
    assert [tuple(row) for row in versions] == [(1, 1)]
    assert all(connection.was_closed for connection in created_connections)


def test_wal_activation_retry_is_bounded_and_closes_connection(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    created_connections = []

    class AlwaysLockedWalConnection(sqlite3.Connection):
        wal_attempts = 0
        was_closed = False

        def execute(self, sql, parameters=(), /):
            if sql == "PRAGMA journal_mode=WAL":
                self.wal_attempts += 1
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, parameters)

        def close(self):
            self.was_closed = True
            super().close()

    def locked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=AlwaysLockedWalConnection)
        created_connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", locked_connect)

    with pytest.raises(
        RuntimeError,
        match=r"Could not enable SQLite WAL mode after \d+ attempts: database is locked",
    ):
        Database(tmp_path / "app.db").migrate()

    assert 1 < created_connections[0].wal_attempts <= 5
    assert created_connections[0].was_closed


def test_migrate_rejects_a_journal_mode_other_than_wal():
    database = Database(Path(":memory:"))

    with pytest.raises(
        RuntimeError,
        match=r"Expected SQLite journal mode 'wal', received 'memory'",
    ):
        database.migrate()


def test_migrate_creates_schema_and_wal(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode == "wal"
    assert {
        "schema_migrations",
        "users",
        "sessions",
        "model_endpoints",
        "datasets",
        "jobs",
        "job_metrics",
        "artifacts",
    }.issubset(tables)


def test_foreign_keys_are_enforced(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()
    with database.connect() as connection:
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert enabled == 1


def test_migrations_are_idempotent_and_record_version_once(tmp_path):
    database = Database(tmp_path / "app.db")

    database.migrate()
    database.migrate()

    with database.connect() as connection:
        applied_versions = connection.execute(
            "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version"
        ).fetchall()
    assert [tuple(row) for row in applied_versions] == [(1, 1)]


def test_migration_creates_required_column_shapes(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    with database.connect() as connection:
        endpoint_columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(model_endpoints)")
        }
        dataset_columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(datasets)")
        }
        job_columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(jobs)")}

    assert (
        endpoint_columns["encrypted_api_key"]["type"],
        endpoint_columns["encrypted_api_key"]["notnull"],
    ) == ("BLOB", 0)
    assert (
        endpoint_columns["request_timeout"]["type"],
        endpoint_columns["request_timeout"]["notnull"],
        endpoint_columns["request_timeout"]["dflt_value"],
    ) == ("INTEGER", 1, "60")
    assert (
        endpoint_columns["max_output_length"]["type"],
        endpoint_columns["max_output_length"]["dflt_value"],
    ) == ("INTEGER", "512")
    assert (
        dataset_columns["size_bytes"]["type"],
        dataset_columns["size_bytes"]["notnull"],
    ) == ("INTEGER", 0)
    assert (
        job_columns["model_snapshot_json"]["type"],
        job_columns["model_snapshot_json"]["notnull"],
    ) == ("TEXT", 1)


def test_users_schema_has_one_unique_persisted_username_key(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    with database.connect() as connection:
        user_columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(users)")}
        unique_application_indexes = {
            tuple(
                column["name"]
                for column in connection.execute(f"PRAGMA index_info({index['name']})")
            )
            for index in connection.execute("PRAGMA index_list(users)")
            if index["unique"] and index["origin"] == "u"
        }

    assert (user_columns["username_key"]["type"], user_columns["username_key"]["notnull"]) == (
        "TEXT",
        1,
    )
    assert unique_application_indexes == {("username_key",)}


def test_migration_creates_required_foreign_key_actions(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    with database.connect() as connection:
        foreign_keys = {
            table: {
                row["from"]: (row["table"], row["to"], row["on_delete"])
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            for table in ("sessions", "model_endpoints", "jobs", "job_metrics", "artifacts")
        }

    assert foreign_keys["sessions"]["user_id"] == ("users", "id", "CASCADE")
    assert foreign_keys["model_endpoints"]["owner_id"] == ("users", "id", "CASCADE")
    assert foreign_keys["jobs"]["owner_id"] == ("users", "id", "CASCADE")
    assert foreign_keys["jobs"]["model_endpoint_id"] == (
        "model_endpoints",
        "id",
        "NO ACTION",
    )
    assert foreign_keys["jobs"]["dataset_id"] == ("datasets", "id", "NO ACTION")
    assert foreign_keys["job_metrics"]["job_id"] == ("jobs", "id", "CASCADE")
    assert foreign_keys["artifacts"]["job_id"] == ("jobs", "id", "CASCADE")


def test_migration_creates_required_explicit_indexes(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    expected_columns = {
        "sessions_user_id_idx": ("user_id",),
        "sessions_expires_at_idx": ("expires_at",),
        "jobs_queue_idx": ("status", "created_at", "id"),
        "jobs_owner_idx": ("owner_id", "created_at"),
        "jobs_model_endpoint_id_idx": ("model_endpoint_id",),
        "jobs_dataset_id_idx": ("dataset_id",),
        "artifacts_job_id_idx": ("job_id",),
    }
    with database.connect() as connection:
        explicit_indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        actual_columns = {
            name: tuple(row["name"] for row in connection.execute(f"PRAGMA index_info({name})"))
            for name in expected_columns
        }
        owner_index = [
            (row["name"], row["desc"])
            for row in connection.execute("PRAGMA index_xinfo(jobs_owner_idx)")
            if row["key"]
        ]
        expiry_query_plan = [
            row["detail"]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM sessions WHERE expires_at <= ?",
                ("2026-01-01T00:00:00+00:00",),
            )
        ]

    assert explicit_indexes == set(expected_columns)
    assert actual_columns == expected_columns
    assert owner_index == [("owner_id", 0), ("created_at", 1)]
    assert any("sessions_expires_at_idx" in detail for detail in expiry_query_plan)


def test_foreign_keys_restrict_referenced_parents_and_cascade_owned_rows(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO users
            VALUES ('user-1', 'alice', 'alice', 'hash', 'created', NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO sessions VALUES (
              'session-1', 'user-1', 'token-hash', 'created', 'expires'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO model_endpoints (
              id, owner_id, name, base_url, model_name, created_at, updated_at
            ) VALUES (
              'endpoint-1', 'user-1', 'model', 'https://example.test', 'model-1',
              'created', 'updated'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO datasets (
              id, config_name, name, description, status, updated_at
            ) VALUES ('dataset-1', 'dataset', 'Dataset', 'description', 'ready', 'updated')
            """
        )
        connection.execute(
            """
            INSERT INTO jobs (
              id, owner_id, model_endpoint_id, dataset_id, mode, status,
              model_snapshot_json, dataset_snapshot_json, parameters_json,
              config_path, output_dir, log_path, created_at, updated_at
            ) VALUES (
              'job-1', 'user-1', 'endpoint-1', 'dataset-1', 'full', 'queued',
              '{}', '{}', '{}', 'config', 'output', 'log', 'created', 'updated'
            )
            """
        )
        connection.execute("INSERT INTO job_metrics VALUES ('job-1', 'accuracy', 1.0, NULL, NULL)")
        connection.execute(
            "INSERT INTO artifacts VALUES ('artifact-1', 'job-1', 'log', 'log.txt', 'text/plain', 'created')"
        )

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute("DELETE FROM model_endpoints WHERE id = 'endpoint-1'")

        connection.execute("DELETE FROM users WHERE id = 'user-1'")
        owned_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("sessions", "model_endpoints", "jobs", "job_metrics", "artifacts")
        }
        dataset_count = connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]

    assert owned_counts == {
        "sessions": 0,
        "model_endpoints": 0,
        "jobs": 0,
        "job_metrics": 0,
        "artifacts": 0,
    }
    assert dataset_count == 1


def test_successful_connection_context_commits(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO users (id, username, username_key, password_hash, created_at)
            VALUES ('user-1', 'alice', 'alice', 'hash', '2026-08-17T00:00:00+00:00')
            """
        )

    with database.connect() as connection:
        user = connection.execute("SELECT username FROM users WHERE id = 'user-1'").fetchone()
    assert user["username"] == "alice"


def test_connection_context_rolls_back_on_exception(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    with (
        pytest.raises(RuntimeError, match="stop transaction"),
        database.connect() as connection,
    ):
        connection.execute(
            """
            INSERT INTO users (id, username, username_key, password_hash, created_at)
            VALUES ('user-1', 'alice', 'alice', 'hash', '2026-08-17T00:00:00+00:00')
            """
        )
        raise RuntimeError("stop transaction")

    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users WHERE id = 'user-1'").fetchone()[0]
    assert count == 0


def test_connect_creates_configured_distinct_connections_and_closes_them(tmp_path):
    database = Database(tmp_path / "app.db")

    with database.connect() as first, database.connect() as second:
        assert first is not second
        assert first.row_factory is sqlite3.Row
        assert second.row_factory is sqlite3.Row
        assert first.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        first.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        second.execute("SELECT 1")


@pytest.mark.asyncio
async def test_fastapi_lifespan_migrates_database_before_service_use(tmp_path):
    settings = Settings.create(tmp_path, tmp_path / "ais_bench", 1)
    app = create_app(settings=settings, start_worker=False)

    assert isinstance(app.state.database, Database)
    assert not settings.db_path.exists()

    async with app.router.lifespan_context(app):
        with app.state.database.connect() as connection:
            migration = connection.execute("SELECT version FROM schema_migrations").fetchone()

    assert migration["version"] == 1

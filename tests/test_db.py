import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from aisbench_web.app import create_app
from aisbench_web.db import MIGRATION_1, Database
from aisbench_web.repositories.users import UserRepository
from aisbench_web.security import hash_password
from aisbench_web.settings import Settings

LEGACY_USERS_SQL = """
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL COLLATE NOCASE UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_login_at TEXT
)
"""


def create_legacy_v1_database(database: Database) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            )
            """
        )
        for statement in MIGRATION_1:
            if "CREATE TABLE users" in statement:
                statement = LEGACY_USERS_SQL
            elif "sessions_expires_at_idx" in statement:
                continue
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (1, "2026-01-01T00:00:00+00:00"),
        )


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
    assert [tuple(row) for row in versions] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1), (8, 1)]
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


def test_migration_1_remains_the_exact_legacy_user_and_session_shape(tmp_path):
    database = Database(tmp_path / "legacy-v1.db")

    with database.connect() as connection:
        for statement in MIGRATION_1:
            connection.execute(statement)
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        indexes = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }

    assert "username_key" not in user_columns
    assert "sessions_expires_at_idx" not in indexes


@pytest.mark.asyncio
async def test_migration_2_upgrades_legacy_users_and_sessions_atomically(tmp_path):
    settings = Settings.create(tmp_path, tmp_path / "ais_bench", 1)
    database = Database(settings.db_path)
    create_legacy_v1_database(database)
    password_hash = hash_password("password one")
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expired_at = created_at - timedelta(days=1)
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO users (id, username, password_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("user-1", "Élodie", password_hash, created_at.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "expired-session",
                "user-1",
                "expired-token-hash",
                created_at.isoformat(),
                expired_at.isoformat(),
            ),
        )

    database.migrate()

    credentials = UserRepository(database).get_credentials_by_username("éLODIE")
    assert credentials is not None
    assert credentials.user.username == "Élodie"
    with database.connect() as connection:
        migrated_user = connection.execute(
            "SELECT username_key FROM users WHERE id = ?", ("user-1",)
        ).fetchone()
        preserved_session = connection.execute(
            "SELECT id FROM sessions WHERE id = ?", ("expired-session",)
        ).fetchone()
    assert migrated_user["username_key"] == "élodie"
    assert preserved_session["id"] == "expired-session"

    app = create_app(settings=settings, start_worker=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "éLODIE", "password": "password one"},
            )
    assert login.status_code == 200
    assert login.json()["username"] == "Élodie"

    with database.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        sessions = connection.execute("SELECT id FROM sessions ORDER BY id").fetchall()
        expiry_query_plan = [
            row["detail"]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN DELETE FROM sessions WHERE expires_at <= ?",
                (datetime.now(timezone.utc).isoformat(),),
            )
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO users (
                  id, username, username_key, password_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("user-2", "Alias", "élodie", "hash", created_at.isoformat()),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="username_key must not be null",
        ):
            connection.execute(
                """
                INSERT INTO users (id, username, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("user-3", "Bob", "hash", created_at.isoformat()),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="username_key must not be null",
        ):
            connection.execute(
                "UPDATE users SET username_key = NULL WHERE id = ?",
                ("user-1",),
            )

    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [row["id"] for row in sessions] != ["expired-session"]
    assert any("sessions_expires_at_idx" in detail for detail in expiry_query_plan)


def test_migration_2_rolls_back_casefold_collisions(tmp_path):
    database = Database(tmp_path / "collision.db")
    create_legacy_v1_database(database)
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO users (id, username, password_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("user-1", "Straße", "hash", "2026-01-01T00:00:00+00:00"),
                ("user-2", "STRASSE", "hash", "2026-01-01T00:00:00+00:00"),
            ],
        )

    with pytest.raises(
        RuntimeError,
        match=r"casefold collision.*Straße.*STRASSE",
    ):
        database.migrate()

    with database.connect() as connection:
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        expiry_index = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("sessions_expires_at_idx",),
        ).fetchone()
        users = connection.execute("SELECT username FROM users ORDER BY id").fetchall()

    assert "username_key" not in user_columns
    assert [row["version"] for row in versions] == [1]
    assert expiry_index is None
    assert [row["username"] for row in users] == ["Straße", "STRASSE"]


def test_migration_2_accepts_transient_hardened_version_1_without_duplicate_indexes(
    tmp_path,
):
    database = Database(tmp_path / "transient.db")
    # A real v1 database always carries every MIGRATION_1 table; only users differs here.
    with database.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            CREATE TABLE users (
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              username_key TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_login_at TEXT
            );
            """
        )
        for statement in MIGRATION_1:
            if "CREATE TABLE users" in statement:
                continue
            connection.execute(statement)
        connection.executescript(
            """
            CREATE INDEX sessions_expires_at_idx ON sessions(expires_at);
            INSERT INTO schema_migrations VALUES (1, '2026-01-01T00:00:00+00:00');
            INSERT INTO users VALUES (
              'user-1', 'Élodie', 'élodie', 'hash', '2026-01-01T00:00:00+00:00', NULL
            );
            """
        )

    database.migrate()

    with database.connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        username_key_indexes = [
            index["name"]
            for index in connection.execute("PRAGMA index_list(users)")
            if index["unique"]
            and tuple(
                row["name"]
                for row in connection.execute(f"PRAGMA index_info({index['name']})")
            )
            == ("username_key",)
        ]

    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert len(username_key_indexes) == 1


def test_migration_5_adds_a_dataset_domain_defaulting_to_uncategorised(tmp_path):
    database = Database(tmp_path / "domains.db")
    database.migrate()

    with database.connect() as connection:
        column = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(datasets)")
        }["category"]

    assert (column["type"], column["notnull"], column["dflt_value"]) == ("TEXT", 1, "'other'")


def test_migration_3_adds_progress_columns_and_leaves_existing_jobs_untouched(tmp_path):
    database = Database(tmp_path / "progress.db")
    database.migrate()

    with database.connect() as connection:
        job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}

    assert {"progress_completed", "progress_total"} <= job_columns


def test_migrate_rejects_database_from_a_newer_schema_version(tmp_path):
    database = Database(tmp_path / "newer.db")
    database.migrate()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (99, "2026-01-01T00:00:00+00:00"),
        )

    with pytest.raises(RuntimeError, match=r"newer schema version 99"):
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
    assert [tuple(row) for row in applied_versions] == [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1), (8, 1)]


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
    # A model endpoint is an address and a key; per-run limits belong to the job.
    assert "request_timeout" not in endpoint_columns
    assert "max_output_length" not in endpoint_columns
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

import sqlite3

import pytest

from aisbench_web.app import create_app
from aisbench_web.db import Database
from aisbench_web.settings import Settings


def test_migrate_creates_schema_and_wal(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
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


def test_successful_connection_context_commits(tmp_path):
    database = Database(tmp_path / "app.db")
    database.migrate()

    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO users (id, username, password_hash, created_at)
            VALUES ('user-1', 'alice', 'hash', '2026-08-17T00:00:00+00:00')
            """
        )

    with database.connect() as connection:
        user = connection.execute(
            "SELECT username FROM users WHERE id = 'user-1'"
        ).fetchone()
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
            INSERT INTO users (id, username, password_hash, created_at)
            VALUES ('user-1', 'alice', 'hash', '2026-08-17T00:00:00+00:00')
            """
        )
        raise RuntimeError("stop transaction")

    with database.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE id = 'user-1'"
        ).fetchone()[0]
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
            migration = connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchone()

    assert migration["version"] == 1

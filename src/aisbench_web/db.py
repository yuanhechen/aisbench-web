import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

WAL_ACTIVATION_ATTEMPTS = 3
WAL_RETRY_BASE_DELAY_SECONDS = 0.01

MIGRATION_1 = (
    """
    CREATE TABLE users (
      id TEXT PRIMARY KEY,
      username TEXT NOT NULL COLLATE NOCASE UNIQUE,
      password_hash TEXT NOT NULL,
      created_at TEXT NOT NULL,
      last_login_at TEXT
    )
    """,
    """
    CREATE TABLE sessions (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX sessions_user_id_idx ON sessions(user_id)",
    """
    CREATE TABLE model_endpoints (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      base_url TEXT NOT NULL,
      model_name TEXT NOT NULL,
      encrypted_api_key BLOB,
      request_timeout INTEGER NOT NULL DEFAULT 60,
      max_output_length INTEGER NOT NULL DEFAULT 512,
      is_active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(owner_id, name)
    )
    """,
    """
    CREATE TABLE datasets (
      id TEXT PRIMARY KEY,
      config_name TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      description TEXT NOT NULL,
      status TEXT NOT NULL,
      local_path TEXT,
      download_url TEXT,
      sha256 TEXT,
      size_bytes INTEGER,
      error_message TEXT,
      installed_at TEXT,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE jobs (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      model_endpoint_id TEXT NOT NULL REFERENCES model_endpoints(id),
      dataset_id TEXT NOT NULL REFERENCES datasets(id),
      mode TEXT NOT NULL,
      status TEXT NOT NULL,
      model_snapshot_json TEXT NOT NULL,
      dataset_snapshot_json TEXT NOT NULL,
      parameters_json TEXT NOT NULL,
      config_path TEXT NOT NULL,
      output_dir TEXT NOT NULL,
      log_path TEXT NOT NULL,
      pid INTEGER,
      exit_code INTEGER,
      error_code TEXT,
      error_message TEXT,
      created_at TEXT NOT NULL,
      started_at TEXT,
      finished_at TEXT,
      updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX jobs_queue_idx ON jobs(status, created_at, id)",
    "CREATE INDEX jobs_owner_idx ON jobs(owner_id, created_at DESC)",
    "CREATE INDEX jobs_model_endpoint_id_idx ON jobs(model_endpoint_id)",
    "CREATE INDEX jobs_dataset_id_idx ON jobs(dataset_id)",
    """
    CREATE TABLE job_metrics (
      job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
      key TEXT NOT NULL,
      value REAL,
      text_value TEXT,
      unit TEXT,
      PRIMARY KEY(job_id, key)
    )
    """,
    """
    CREATE TABLE artifacts (
      id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
      kind TEXT NOT NULL,
      relative_path TEXT NOT NULL,
      content_type TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX artifacts_job_id_idx ON artifacts(job_id)",
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            self._enable_wal(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            already_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 1"
            ).fetchone()
            if already_applied is not None:
                return

            for statement in MIGRATION_1:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (1, datetime.now(timezone.utc).isoformat()),
            )

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        last_error = None
        for attempt in range(1, WAL_ACTIVATION_ATTEMPTS + 1):
            try:
                journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold():
                    raise
                last_error = exc
                if attempt < WAL_ACTIVATION_ATTEMPTS:
                    time.sleep(WAL_RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1))
                continue

            if journal_mode != "wal":
                raise RuntimeError(
                    f"Expected SQLite journal mode 'wal', received {journal_mode!r}"
                )
            return

        raise RuntimeError(
            "Could not enable SQLite WAL mode "
            f"after {WAL_ACTIVATION_ATTEMPTS} attempts: {last_error}"
        ) from last_error

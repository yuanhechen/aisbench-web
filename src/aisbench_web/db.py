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

# Version 1 shipped a ``username COLLATE NOCASE UNIQUE`` column. SQLite's NOCASE collation only
# folds ASCII, so "Élodie" and "élodie" registered as two accounts. Version 2 rebuilds the table
# around a persisted ``username_key`` holding Python's ``str.casefold()`` of the display name.
MIGRATION_2_USERS_TABLE = """
CREATE TABLE users_migration_2 (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  username_key TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_login_at TEXT
)
"""

# A bare NOT NULL reports "NOT NULL constraint failed: users.username_key", which does not say
# which invariant a caller broke. BEFORE triggers run ahead of column constraints, so these keep
# the column NOT NULL while raising the intent.
MIGRATION_2_TRIGGERS = (
    """
    CREATE TRIGGER users_username_key_not_null_insert
    BEFORE INSERT ON users
    FOR EACH ROW WHEN NEW.username_key IS NULL
    BEGIN
      SELECT RAISE(ABORT, 'username_key must not be null');
    END
    """,
    """
    CREATE TRIGGER users_username_key_not_null_update
    BEFORE UPDATE OF username_key ON users
    FOR EACH ROW WHEN NEW.username_key IS NULL
    BEGIN
      SELECT RAISE(ABORT, 'username_key must not be null');
    END
    """,
)

# Progress is shown on a page that can be refreshed at any moment, so it has to survive
# outside the WebSocket that reports it live.
MIGRATION_3 = (
    "ALTER TABLE jobs ADD COLUMN progress_completed INTEGER",
    "ALTER TABLE jobs ADD COLUMN progress_total INTEGER",
)

LATEST_SCHEMA_VERSION = 3


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
            # Rebuilding `users` drops and renames a table that `sessions`, `model_endpoints`, and
            # `jobs` reference. Foreign keys cannot be toggled inside a transaction, so this has to
            # happen before BEGIN; `PRAGMA foreign_key_check` re-validates before the commit, and
            # every other connection re-enables enforcement in `connect()`.
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            newest_applied = max(applied, default=0)
            if newest_applied > LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database at {self.path} has a newer schema version {newest_applied} "
                    f"than this AISBench Web release supports "
                    f"(maximum {LATEST_SCHEMA_VERSION}); upgrade aisbench-web"
                )

            migrations = (
                (1, self._apply_migration_1),
                (2, self._apply_migration_2),
                (3, self._apply_migration_3),
            )
            pending = [(version, apply) for version, apply in migrations if version not in applied]
            if not pending:
                return

            for version, apply in pending:
                apply(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(timezone.utc).isoformat()),
                )

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Migrating {self.path} left {len(violations)} foreign key violations, "
                    f"starting with table {violations[0][0]!r}"
                )

    @staticmethod
    def _apply_migration_1(connection: sqlite3.Connection) -> None:
        for statement in MIGRATION_1:
            connection.execute(statement)

    @staticmethod
    def _apply_migration_3(connection: sqlite3.Connection) -> None:
        for statement in MIGRATION_3:
            connection.execute(statement)

    @staticmethod
    def _apply_migration_2(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id, username, password_hash, created_at, last_login_at
            FROM users
            ORDER BY id
            """
        ).fetchall()

        by_key: dict[str, list[str]] = {}
        for row in rows:
            by_key.setdefault(row["username"].casefold(), []).append(row["username"])
        for username_key, usernames in by_key.items():
            if len(usernames) > 1:
                collided = " and ".join(repr(username) for username in usernames)
                raise RuntimeError(
                    f"Cannot upgrade to schema version 2: username casefold collision "
                    f"on {username_key!r} between {collided}; rename all but one account "
                    f"before upgrading aisbench-web"
                )

        connection.execute(MIGRATION_2_USERS_TABLE)
        connection.executemany(
            """
            INSERT INTO users_migration_2 (
              id, username, username_key, password_hash, created_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["id"],
                    row["username"],
                    row["username"].casefold(),
                    row["password_hash"],
                    row["created_at"],
                    row["last_login_at"],
                )
                for row in rows
            ],
        )
        connection.execute("DROP TABLE users")
        connection.execute("ALTER TABLE users_migration_2 RENAME TO users")
        for statement in MIGRATION_2_TRIGGERS:
            connection.execute(statement)

        # Expired sessions are cleared on the next login, not here: an interrupted upgrade should
        # not be the reason a signed-in user is logged out.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions(expires_at)"
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
                raise RuntimeError(f"Expected SQLite journal mode 'wal', received {journal_mode!r}")
            return

        raise RuntimeError(
            "Could not enable SQLite WAL mode "
            f"after {WAL_ACTIVATION_ATTEMPTS} attempts: {last_error}"
        ) from last_error

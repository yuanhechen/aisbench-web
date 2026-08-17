import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from aisbench_web.db import Database
from aisbench_web.security import SESSION_DAYS


class DuplicateUsernameError(Exception):
    """Raised when a case-insensitive username is already registered."""


@dataclass(frozen=True)
class User:
    id: str
    username: str
    created_at: str
    last_login_at: str | None


@dataclass(frozen=True)
class UserCredentials:
    user: User
    password_hash: str


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    token_hash: str
    created_at: str
    expires_at: str


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_user_with_session(
        self,
        *,
        username: str,
        password_hash: str,
        token_hash: str,
        now: datetime | None = None,
    ) -> tuple[User, Session]:
        timestamp = self._utc_now(now)
        user = User(
            id=str(uuid4()),
            username=username,
            created_at=timestamp.isoformat(),
            last_login_at=None,
        )
        session = self._new_session(user.id, token_hash, timestamp)

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_usernames = connection.execute("SELECT username FROM users").fetchall()
            if any(row["username"].casefold() == username.casefold() for row in existing_usernames):
                raise DuplicateUsernameError(username)
            try:
                connection.execute(
                    """
                    INSERT INTO users (id, username, password_hash, created_at, last_login_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.username,
                        password_hash,
                        user.created_at,
                        user.last_login_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "users.username" not in str(exc):
                    raise
                raise DuplicateUsernameError(username) from exc
            self._insert_session(connection, session)
        return user, session

    def get_credentials_by_username(self, username: str) -> UserCredentials | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, username, password_hash, created_at, last_login_at
                FROM users
                WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            ).fetchone()
            if row is None:
                rows = connection.execute(
                    """
                    SELECT id, username, password_hash, created_at, last_login_at
                    FROM users
                    """
                ).fetchall()
                row = next(
                    (
                        candidate
                        for candidate in rows
                        if candidate["username"].casefold() == username.casefold()
                    ),
                    None,
                )
        if row is None:
            return None
        return UserCredentials(
            user=self._user_from_row(row),
            password_hash=row["password_hash"],
        )

    def record_login_and_create_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        now: datetime | None = None,
    ) -> tuple[User, Session]:
        timestamp = self._utc_now(now)
        last_login_at = timestamp.isoformat()
        session = self._new_session(user_id, token_hash, timestamp)

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (last_login_at, user_id),
            )
            self._insert_session(connection, session)
            row = connection.execute(
                """
                SELECT id, username, created_at, last_login_at
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"Unknown user ID: {user_id}")
        return self._user_from_row(row), session

    def get_user_by_session_hash(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> User | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                  users.id,
                  users.username,
                  users.created_at,
                  users.last_login_at,
                  sessions.expires_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None or expires_at <= self._utc_now(now):
            return None
        return self._user_from_row(row)

    def revoke_session(self, token_hash: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (token_hash,),
            )

    @staticmethod
    def _utc_now(now: datetime | None) -> datetime:
        timestamp = now or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _new_session(user_id: str, token_hash: str, now: datetime) -> Session:
        return Session(
            id=str(uuid4()),
            user_id=user_id,
            token_hash=token_hash,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=SESSION_DAYS)).isoformat(),
        )

    @staticmethod
    def _insert_session(connection: sqlite3.Connection, session: Session) -> None:
        connection.execute(
            """
            INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.user_id,
                session.token_hash,
                session.created_at,
                session.expires_at,
            ),
        )

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

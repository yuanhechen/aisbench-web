import hashlib
import secrets
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

SESSION_COOKIE = "aisbench_session"
SESSION_DAYS = 14
PASSWORD_HASH_CONCURRENCY = 2
USERNAME_MAX_LENGTH = 128
PASSWORD_MAX_LENGTH = 1024

_PASSWORD_HASHER = PasswordHasher()
_PASSWORD_WORK_LIMIT = threading.BoundedSemaphore(PASSWORD_HASH_CONCURRENCY)


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must contain at least 8 characters")
    with _PASSWORD_WORK_LIMIT:
        return _PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    with _PASSWORD_WORK_LIMIT:
        try:
            return _PASSWORD_HASHER.verify(encoded, password)
        except VerifyMismatchError:
            return False
        except (VerificationError, InvalidHashError):
            return False


def session_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    digest = session_token_digest(token)
    return token, digest

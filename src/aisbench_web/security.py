import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SESSION_COOKIE = "aisbench_session"
SESSION_DAYS = 14


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must contain at least 8 characters")
    return PasswordHasher().hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return PasswordHasher().verify(encoded, password)
    except VerifyMismatchError:
        return False


def new_session_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, digest

import base64
import hashlib
import os
import secrets
import tempfile
import threading
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SESSION_COOKIE = "aisbench_session"
SESSION_DAYS = 14
PASSWORD_HASH_CONCURRENCY = 2
USERNAME_MAX_LENGTH = 128
PASSWORD_MAX_LENGTH = 1024
SECRET_BYTES = 64
API_KEY_ENCRYPTION_INFO = b"aisbench-web model endpoint api key"

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


def _read_secret(path: Path) -> bytes | None:
    try:
        secret = path.read_bytes()
    except FileNotFoundError:
        return None
    return secret if len(secret) >= SECRET_BYTES else None


def load_or_create_secret(path: Path) -> bytes:
    """Return the data-directory secret, creating it once with owner-only permissions."""
    secret = _read_secret(path)
    if secret is not None:
        return secret

    # Write the secret in full under a private temporary name, then hard-link it into place.
    # os.link fails rather than overwrites, so a concurrent starter cannot observe a partial
    # file and two starters cannot disagree about which secret won.
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".secret-")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(secrets.token_bytes(SECRET_BYTES))
        temporary_path.chmod(0o600)
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)

    secret = _read_secret(path)
    if secret is None:
        raise RuntimeError(f"Could not initialize the AISBench Web secret at {path}")
    return secret


def api_key_cipher(secret: bytes) -> Fernet:
    """Derive the model-API-key cipher so the stored secret is never used directly."""
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=API_KEY_ENCRYPTION_INFO,
    ).derive(secret)
    return Fernet(base64.urlsafe_b64encode(derived))

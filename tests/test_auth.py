import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from aisbench_web.app import create_app
from aisbench_web.settings import Settings

SESSION_COOKIE = "aisbench_session"
SESSION_MAX_AGE = 14 * 24 * 60 * 60


@pytest.fixture
def auth_app(tmp_path: Path) -> FastAPI:
    settings = Settings.create(tmp_path, tmp_path / "ais_bench", 1)
    return create_app(settings=settings, start_worker=False)


@pytest_asyncio.fixture
async def client(auth_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with auth_app.router.lifespan_context(auth_app):
        transport = httpx.ASGITransport(app=auth_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as test_client:
            yield test_client


def assert_session_cookie(response: httpx.Response, *, secure: bool) -> None:
    set_cookie = response.headers["set-cookie"].casefold()
    assert set_cookie.startswith(f"{SESSION_COOKIE}=")
    assert "; httponly" in set_cookie
    assert "; samesite=lax" in set_cookie
    assert "; path=/" in set_cookie
    assert f"; max-age={SESSION_MAX_AGE}" in set_cookie
    assert "; expires=" in set_cookie
    assert ("; secure" in set_cookie) is secure


@pytest.mark.asyncio
async def test_registration_returns_user_and_sets_scheme_appropriate_cookie(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    http_response = await client.post(
        "/api/auth/register",
        json={"username": "Alice", "password": "correct horse battery staple"},
    )

    assert http_response.status_code == 201
    assert http_response.json()["username"] == "Alice"
    assert (
        not {
            "password",
            "password_hash",
            "session",
            "session_token",
            "role",
            "admin",
            "invite",
        }
        & http_response.json().keys()
    )
    assert_session_cookie(http_response, secure=False)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as https:
        https_response = await https.post(
            "/api/auth/register",
            json={"username": "Bob", "password": "another good password"},
        )

    assert https_response.status_code == 201
    assert_session_cookie(https_response, secure=True)


@pytest.mark.asyncio
async def test_registration_rejects_case_insensitive_duplicate(
    client: httpx.AsyncClient,
) -> None:
    first = await client.post(
        "/api/auth/register",
        json={"username": "CaseSensitiveDisplay", "password": "password one"},
    )
    duplicate = await client.post(
        "/api/auth/register",
        json={"username": "casesensitivedisplay", "password": "password two"},
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_username_case_insensitivity_supports_unicode(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    registration = await client.post(
        "/api/auth/register",
        json={"username": "Élodie", "password": "password one"},
    )
    client.cookies.clear()
    login = await client.post(
        "/api/auth/login",
        json={"username": "élodie", "password": "password one"},
    )
    client.cookies.clear()
    duplicate = await client.post(
        "/api/auth/register",
        json={"username": "éLODIE", "password": "password two"},
    )

    assert registration.status_code == 201
    assert login.status_code == 200
    assert login.json()["username"] == "Élodie"
    assert duplicate.status_code == 409
    with auth_app.state.database.connect() as connection:
        stored_user = connection.execute("SELECT username, username_key FROM users").fetchone()
    assert tuple(stored_user) == ("Élodie", "élodie")


@pytest.mark.asyncio
async def test_concurrent_unicode_equivalent_registrations_create_one_user(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    transport = httpx.ASGITransport(app=auth_app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as first,
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as second,
    ):
        responses = await asyncio.gather(
            first.post(
                "/api/auth/register",
                json={"username": "Straße", "password": "password one"},
            ),
            second.post(
                "/api/auth/register",
                json={"username": "STRASSE", "password": "password two"},
            ),
        )

    assert sorted(response.status_code for response in responses) == [201, 409]
    with auth_app.state.database.connect() as connection:
        users = connection.execute("SELECT username, username_key FROM users").fetchall()
    assert len(users) == 1
    assert users[0]["username_key"] == "strasse"


@pytest.mark.asyncio
async def test_short_password_is_validation_error_and_inserts_no_user(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    password = "s3cr3t"
    response = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": password},
    )

    assert response.status_code == 422
    assert password not in response.text
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("u" * 129, "password one"),
        ("alice", "sensitive-registration-marker-" + "x" * 1_024),
    ],
)
async def test_oversized_registration_input_is_rejected_before_hashing(
    username: str,
    password: str,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisbench_web.api import auth as auth_api

    hash_calls: list[str] = []

    def record_hash(value: str) -> str:
        hash_calls.append(value)
        return "unused hash"

    monkeypatch.setattr(auth_api, "hash_password", record_hash)

    response = await client.post(
        "/api/auth/register",
        json={"username": username, "password": password},
    )

    assert response.status_code == 422
    assert hash_calls == []
    assert password not in response.text


@pytest.mark.asyncio
async def test_oversized_login_password_is_rejected_before_verification(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisbench_web.api import auth as auth_api

    password = "sensitive-login-marker-" + "x" * 1_024
    verification_calls: list[tuple[str, str]] = []

    def record_verification(encoded: str, value: str) -> bool:
        verification_calls.append((encoded, value))
        return False

    monkeypatch.setattr(auth_api, "verify_password", record_verification)

    response = await client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": password},
    )

    assert response.status_code == 422
    assert verification_calls == []
    assert password not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "password"),
    [
        ("/api/auth/register", {"secret": "registration-password-marker"}),
        ("/api/auth/login", ["login-password-marker"]),
    ],
)
async def test_invalid_password_types_are_not_reflected_in_validation_errors(
    path: str,
    password: object,
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        path,
        json={"username": "alice", "password": password},
    )

    assert response.status_code == 422
    assert "password-marker" not in response.text
    with auth_app.state.database.connect() as connection:
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert user_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/auth/register",
            '{"username":"\\ud800alice","password":"password one"}',
        ),
        (
            "/api/auth/register",
            '{"username":"alice","password":"\\ud800password"}',
        ),
        (
            "/api/auth/login",
            '{"username":"\\ud800alice","password":"password one"}',
        ),
        (
            "/api/auth/login",
            '{"username":"alice","password":"\\ud800password"}',
        ),
    ],
)
async def test_non_utf8_text_is_rejected_before_password_work(
    path: str,
    body: str,
    auth_app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisbench_web.api import auth as auth_api

    password_work: list[str] = []

    def record_hash(password: str) -> str:
        password_work.append(password)
        return "unused hash"

    def record_verification(encoded: str, password: str) -> bool:
        password_work.append(password)
        return False

    monkeypatch.setattr(auth_api, "hash_password", record_hash)
    monkeypatch.setattr(auth_api, "verify_password", record_verification)

    response = await client.post(
        path,
        content=body.encode("ascii"),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert password_work == []
    with auth_app.state.database.connect() as connection:
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert user_count == 0


def test_malformed_password_hash_is_a_failed_verification() -> None:
    from aisbench_web.security import verify_password

    assert not verify_password("not-an-argon2-hash", "password one")


def test_password_work_is_limited_to_two_concurrent_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from aisbench_web import security

    state_lock = threading.Lock()
    two_entered = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0

    class TrackingHasher:
        def _work(self) -> None:
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
                if active >= 2:
                    two_entered.set()
            release.wait(timeout=2)
            with state_lock:
                active -= 1

        def hash(self, password: str) -> str:
            self._work()
            return f"encoded:{password}"

        def verify(self, encoded: str, password: str) -> bool:
            self._work()
            return encoded == f"encoded:{password}"

    tracking_hasher = TrackingHasher()
    monkeypatch.setattr(security, "_PASSWORD_HASHER", tracking_hasher, raising=False)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(
                security.hash_password if index % 2 == 0 else security.verify_password,
                *("password one",) if index % 2 == 0 else ("encoded:password one", "password one"),
            )
            for index in range(6)
        ]
        assert two_entered.wait(timeout=1)
        threading.Event().wait(0.05)
        with state_lock:
            observed_max_active = max_active
        release.set()
        for future in futures:
            future.result(timeout=2)

    assert observed_max_active == 2


@pytest.mark.asyncio
async def test_me_requires_cookie_and_succeeds_immediately_after_registration(
    client: httpx.AsyncClient,
) -> None:
    before = await client.get("/api/me")
    registration = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
    )
    after = await client.get("/api/me")

    assert before.status_code == 401
    assert registration.status_code == 201
    assert after.status_code == 200
    assert after.json() == registration.json()


@pytest.mark.asyncio
async def test_login_is_case_insensitive_and_uses_generic_invalid_credentials(
    client: httpx.AsyncClient,
) -> None:
    registration = await client.post(
        "/api/auth/register",
        json={"username": "Alice", "password": "password one"},
    )
    assert registration.status_code == 201
    client.cookies.clear()

    wrong_password = await client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "wrong password"},
    )
    unknown_user = await client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong password"},
    )
    success = await client.post(
        "/api/auth/login",
        json={"username": "aLiCe", "password": "password one"},
    )

    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json() == {"detail": "invalid credentials"}
    assert success.status_code == 200
    assert success.json()["username"] == "Alice"
    assert success.json()["last_login_at"] is not None
    assert_session_cookie(success, secure=False)


@pytest.mark.asyncio
async def test_unknown_username_still_performs_password_verification(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisbench_web.api import auth as auth_api

    verification_calls: list[tuple[str, str]] = []

    def record_verification(encoded: str, password: str) -> bool:
        verification_calls.append((encoded, password))
        return False

    monkeypatch.setattr(auth_api, "verify_password", record_verification)

    response = await client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong password"},
    )

    assert response.status_code == 401
    assert len(verification_calls) == 1
    assert verification_calls[0][0].startswith("$argon2")


@pytest.mark.asyncio
async def test_logout_revokes_session_and_old_cookie_cannot_be_replayed(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    registration = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
    )
    assert registration.status_code == 201
    raw_token = client.cookies[SESSION_COOKIE]
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with auth_app.state.database.connect() as connection:
        count_before = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()[0]

    logout = await client.post("/api/auth/logout")

    assert count_before == 1
    assert logout.status_code == 204
    assert logout.content == b""
    assert SESSION_COOKIE not in client.cookies
    assert "max-age=0" in logout.headers["set-cookie"].casefold()
    with auth_app.state.database.connect() as connection:
        count_after = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()[0]
    assert count_after == 0

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Cookie": f"{SESSION_COOKIE}={raw_token}"},
    ) as replay_client:
        replay = await replay_client.get("/api/me")
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_rejected(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    registration = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
    )
    assert registration.status_code == 201
    raw_token = client.cookies[SESSION_COOKIE]
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with auth_app.state.database.connect() as connection:
        connection.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (expired_at, token_hash),
        )

    response = await client.get("/api/me")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_creating_session_removes_expired_sessions(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    from aisbench_web.security import session_token_digest

    registration = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
    )
    assert registration.status_code == 201
    expired_token_hash = session_token_digest(client.cookies[SESSION_COOKIE])
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with auth_app.state.database.connect() as connection:
        connection.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (expired_at, expired_token_hash),
        )

    client.cookies.clear()
    login = await client.post(
        "/api/auth/login",
        json={"username": "Alice", "password": "password one"},
    )

    assert login.status_code == 200
    with auth_app.state.database.connect() as connection:
        expired_count = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE token_hash = ?",
            (expired_token_hash,),
        ).fetchone()[0]
        session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert expired_count == 0
    assert session_count == 1


def test_new_session_token_uses_shared_digest_function() -> None:
    from aisbench_web.security import new_session_token, session_token_digest

    token, digest = new_session_token()

    assert digest == session_token_digest(token)
    assert len(digest) == 64


@pytest.mark.asyncio
async def test_database_contains_only_password_hash_and_session_digest(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    from aisbench_web.security import verify_password

    plaintext_password = "unique plaintext password"
    registration = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": plaintext_password},
    )
    assert registration.status_code == 201
    raw_token = client.cookies[SESSION_COOKIE]

    with auth_app.state.database.connect() as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM users WHERE username = ?", ("alice",)
        ).fetchone()[0]
        token_hash = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]

    assert plaintext_password not in password_hash
    assert raw_token != token_hash
    assert verify_password(password_hash, plaintext_password)
    assert token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    database_bytes = auth_app.state.settings.db_path.read_bytes()
    assert plaintext_password.encode("utf-8") not in database_bytes
    assert raw_token.encode("utf-8") not in database_bytes


@pytest.mark.asyncio
async def test_post_origin_must_be_well_formed_and_match_request_authority(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    payload = {"username": "alice", "password": "password one"}

    different = await client.post(
        "/api/auth/register",
        json=payload,
        headers={"Origin": "https://attacker.example"},
    )
    malformed = await client.post(
        "/api/auth/register",
        json=payload,
        headers={"Origin": "not-an-origin"},
    )
    null_origin = await client.post(
        "/api/auth/register",
        json=payload,
        headers={"Origin": "null"},
    )

    assert different.status_code == 403
    assert malformed.status_code == 403
    assert null_origin.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0

    matching = await client.post(
        "/api/auth/register",
        json=payload,
        headers={"Origin": "HTTP://TESTSERVER"},
    )
    missing = await client.post(
        "/api/auth/register",
        json={"username": "bob", "password": "password two"},
    )
    assert matching.status_code == 201
    assert missing.status_code == 201


@pytest.mark.asyncio
async def test_origin_scheme_must_match_request_scheme(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
        headers={"Origin": "https://testserver"},
    )

    assert response.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "headers"),
    [
        ("http://testserver", {"Origin": "http://testserver:80"}),
        ("https://testserver", {"Origin": "https://testserver:443"}),
        (
            "http://testserver",
            {"Origin": "http://testserver", "Host": "testserver:80"},
        ),
    ],
)
async def test_origin_default_ports_are_canonicalized(
    base_url: str,
    headers: dict[str, str],
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as authority_client:
        response = await authority_client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "password one"},
            headers=headers,
        )

    assert response.status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hosts",
    [
        ["testserver", "testserver"],
        ["testserver", "attacker.example"],
    ],
)
async def test_origin_bearing_mutation_rejects_duplicate_host_headers(
    hosts: list[str],
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
        headers=[("Origin", "http://testserver"), *(("Host", host) for host in hosts)],
    )

    assert response.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_origin_bearing_mutation_rejects_missing_host_header(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    request = client.build_request(
        "POST",
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
        headers={"Origin": "http://testserver"},
    )
    del request.headers["host"]

    response = await client.send(request)

    assert response.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver?",
        "http://testserver#",
        "http://testserver/path",
        "http://user@testserver",
        "http://testserver,https://attacker.example",
    ],
)
async def test_serialized_origin_must_contain_only_one_authority(
    origin: str,
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
        headers={"Origin": origin},
    )

    assert response.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origins",
    [
        ["http://testserver", "http://testserver"],
        ["http://testserver", "https://attacker.example"],
    ],
)
async def test_duplicate_origin_header_fields_are_rejected(
    origins: list[str],
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
        headers=[("Origin", origin) for origin in origins],
    )

    assert response.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "host"),
    [
        (b"\x00http://testserver", b"testserver"),
        (b"http://\xdf", b"ss"),
        (b"http://testserver%", b"testserver%"),
        (b"http://testserver%GG", b"testserver%GG"),
        (b"http://testserver:", b"testserver:"),
        (b"http://testserver:not-a-port", b"testserver:not-a-port"),
        (b"http://testserver:65536", b"testserver:65536"),
        (b"http://[::1", b"[::1"),
        (b"http://[::1]extra", b"[::1]extra"),
        (b"http://[fe80::1%eth\\0]", b"[fe80::1%eth\\0]"),
    ],
)
async def test_origin_serialization_rejects_malformed_authorities(
    origin: bytes,
    host: bytes,
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
        headers=[(b"Origin", origin), (b"Host", host)],
    )

    assert response.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "origin"),
    [
        ("http://example.test:8443", "HTTP://EXAMPLE.TEST:8443"),
        ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
        ("http://[::1]:8123", "HTTP://[::1]:8123"),
    ],
)
async def test_origin_validation_preserves_valid_authorities(
    base_url: str,
    origin: str,
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as authority_client:
        response = await authority_client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "password one"},
            headers={"Origin": origin},
        )

    assert response.status_code == 201
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 1


@pytest.mark.asyncio
async def test_origin_validation_rejects_overlong_host_port_without_error(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
        headers=[
            (b"Origin", b"http://testserver"),
            (b"Host", b"testserver:" + b"9" * 5_000),
        ],
    )

    assert response.status_code == 403
    with auth_app.state.database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_get_api_is_not_subject_to_origin_check(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/me",
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_repository_persists_uuid_and_utc_iso_timestamps_for_fourteen_days(
    auth_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    before = datetime.now(timezone.utc)
    registration = await client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "password one"},
    )
    after = datetime.now(timezone.utc)
    assert registration.status_code == 201

    with auth_app.state.database.connect() as connection:
        user = connection.execute("SELECT id, created_at, last_login_at FROM users").fetchone()
        session = connection.execute(
            "SELECT id, user_id, created_at, expires_at FROM sessions"
        ).fetchone()

    UUID(user["id"])
    UUID(session["id"])
    assert session["user_id"] == user["id"]
    user_created_at = datetime.fromisoformat(user["created_at"])
    session_created_at = datetime.fromisoformat(session["created_at"])
    expires_at = datetime.fromisoformat(session["expires_at"])
    assert user_created_at.tzinfo == timezone.utc
    assert session_created_at.tzinfo == timezone.utc
    assert expires_at.tzinfo == timezone.utc
    assert before <= user_created_at <= after
    assert before <= session_created_at <= after
    assert user["last_login_at"] is None
    assert expires_at - session_created_at == timedelta(days=14)

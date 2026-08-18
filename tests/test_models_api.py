import httpx
import pytest
from fastapi import FastAPI

from aisbench_web.db import Database
from aisbench_web.security import api_key_cipher, load_or_create_secret
from aisbench_web.settings import Settings
from conftest import ClientFactory

ENDPOINT_PAYLOAD = {
    "name": "qwen",
    "host": "127.0.0.1",
    "port": 8001,
    "api_key": "secret-token",
    "request_timeout": 60,
    "max_output_length": 512,
}
MODEL_LISTING = {"data": [{"id": "Qwen3-32B"}, {"id": "another-model"}]}
RESPONSE_FIELDS = {
    "id",
    "name",
    "host",
    "port",
    "use_https",
    "base_url",
    "model_name",
    "has_api_key",
    "request_timeout",
    "max_output_length",
    "is_active",
}


def serve_model_listing(api_app: FastAPI) -> list[httpx.Request]:
    """Answer the model listing the way an OpenAI-compatible service does."""
    return install_probe_transport(
        api_app, lambda _request: httpx.Response(200, json=MODEL_LISTING)
    )


def install_probe_transport(api_app: FastAPI, handler) -> list[httpx.Request]:
    """Route the endpoint prober at an in-memory handler and record its requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    api_app.state.http_transport = httpx.MockTransport(record)
    return seen


@pytest.fixture(autouse=True)
def default_model_listing(api_app: FastAPI) -> None:
    serve_model_listing(api_app)


@pytest.mark.asyncio
async def test_model_endpoints_are_private_between_users(
    client_factory: ClientFactory,
) -> None:
    alice = await client_factory("alice")
    bob = await client_factory("bob")

    created = (await alice.post("/api/models", json=ENDPOINT_PAYLOAD)).json()

    assert (await bob.get("/api/models")).json() == []
    assert (await bob.get(f"/api/models/{created['id']}")).status_code == 404
    assert (
        await bob.patch(f"/api/models/{created['id']}", json={"name": "stolen"})
    ).status_code == 404
    assert (await bob.post(f"/api/models/{created['id']}/test")).status_code == 404
    assert [endpoint["id"] for endpoint in (await alice.get("/api/models")).json()] == [
        created["id"]
    ]


@pytest.mark.asyncio
async def test_api_key_is_encrypted_and_never_returned(
    client: httpx.AsyncClient,
    database: Database,
    settings: Settings,
) -> None:
    response = await client.post("/api/models", json=ENDPOINT_PAYLOAD)

    assert response.status_code == 201
    assert response.json()["has_api_key"] is True
    assert "api_key" not in response.json()

    with database.connect() as connection:
        stored = connection.execute("SELECT encrypted_api_key FROM model_endpoints").fetchone()[0]

    assert isinstance(stored, bytes)
    assert b"secret-token" not in stored
    cipher = api_key_cipher(load_or_create_secret(settings.secret_path))
    assert cipher.decrypt(stored).decode() == "secret-token"


@pytest.mark.asyncio
async def test_response_exposes_exactly_the_agreed_fields(client: httpx.AsyncClient) -> None:
    created = await client.post("/api/models", json=ENDPOINT_PAYLOAD)
    listed = await client.get("/api/models")
    fetched = await client.get(f"/api/models/{created.json()['id']}")

    assert created.json().keys() == RESPONSE_FIELDS
    assert listed.json()[0].keys() == RESPONSE_FIELDS
    assert fetched.json() == created.json()
    assert created.json()["is_active"] is True
    assert created.json()["host"] == "127.0.0.1"
    assert created.json()["port"] == 8001
    assert created.json()["use_https"] is False
    # Derived, not asked for: the user gave a host and a port.
    assert created.json()["base_url"] == "http://127.0.0.1:8001/v1"


@pytest.mark.asyncio
async def test_secret_key_is_created_once_with_owner_only_permissions(
    client: httpx.AsyncClient,
    settings: Settings,
) -> None:
    await client.post("/api/models", json=ENDPOINT_PAYLOAD)
    secret = settings.secret_path.read_bytes()

    await client.post("/api/models", json={**ENDPOINT_PAYLOAD, "name": "second"})

    assert settings.secret_path.stat().st_mode & 0o777 == 0o600
    assert settings.secret_path.read_bytes() == secret
    assert len(secret) >= 32


@pytest.mark.asyncio
async def test_endpoint_without_api_key_reports_no_key(client: httpx.AsyncClient) -> None:
    payload = {key: value for key, value in ENDPOINT_PAYLOAD.items() if key != "api_key"}

    response = await client.post("/api/models", json=payload)

    assert response.status_code == 201
    assert response.json()["has_api_key"] is False


@pytest.mark.asyncio
async def test_names_are_unique_per_owner_only(client_factory: ClientFactory) -> None:
    alice = await client_factory("alice")
    bob = await client_factory("bob")

    assert (await alice.post("/api/models", json=ENDPOINT_PAYLOAD)).status_code == 201
    duplicate = await alice.post("/api/models", json=ENDPOINT_PAYLOAD)

    assert duplicate.status_code == 409
    assert (await bob.post("/api/models", json=ENDPOINT_PAYLOAD)).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"host": ""},
        {"host": "   "},
        {"host": "http://127.0.0.1"},
        {"host": "127.0.0.1:8001"},
        {"port": 0},
        {"port": 70000},
        {"request_timeout": 0},
        {"request_timeout": 601},
        {"max_output_length": 0},
        {"max_output_length": 131073},
        {"name": "x" * 200},
    ],
)
async def test_invalid_endpoint_fields_are_rejected(
    client: httpx.AsyncClient,
    override: dict,
) -> None:
    response = await client.post("/api/models", json={**ENDPOINT_PAYLOAD, **override})

    assert response.status_code == 422
    assert "secret-token" not in response.text


@pytest.mark.asyncio
async def test_model_name_is_detected_from_the_service(client: httpx.AsyncClient) -> None:
    """The user configures where the service is; what it serves is asked of the service."""
    created = await client.post("/api/models", json=ENDPOINT_PAYLOAD)

    assert created.status_code == 201
    assert created.json()["model_name"] == "Qwen3-32B"


@pytest.mark.asyncio
async def test_detection_asks_the_service_with_the_stored_key(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    requests = serve_model_listing(api_app)

    await client.post("/api/models", json=ENDPOINT_PAYLOAD)

    assert str(requests[0].url) == "http://127.0.0.1:8001/v1/models"
    assert requests[0].headers["authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_an_unreachable_service_still_saves_with_no_model_name(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """A temporarily unreachable endpoint must not block saving (design section 7.1)."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    install_probe_transport(api_app, refuse)

    created = await client.post("/api/models", json=ENDPOINT_PAYLOAD)

    assert created.status_code == 201
    assert created.json()["model_name"] == ""


@pytest.mark.asyncio
async def test_testing_the_connection_refreshes_the_detected_model(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    created = (await client.post("/api/models", json=ENDPOINT_PAYLOAD)).json()
    install_probe_transport(
        api_app,
        lambda _request: httpx.Response(200, json={"data": [{"id": "Qwen3-Next"}]}),
    )

    probe = await client.post(f"/api/models/{created['id']}/test")

    assert probe.json()["ok"] is True
    assert probe.json()["models"] == ["Qwen3-Next"]
    refreshed = await client.get(f"/api/models/{created['id']}")
    assert refreshed.json()["model_name"] == "Qwen3-Next"


@pytest.mark.asyncio
async def test_https_endpoints_are_supported(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/models", json={**ENDPOINT_PAYLOAD, "host": "api.example.com", "port": 443,
                             "use_https": True}
    )

    assert created.json()["base_url"] == "https://api.example.com:443/v1"
    assert created.json()["use_https"] is True


@pytest.mark.asyncio
async def test_a_blank_name_falls_back_to_the_address(client: httpx.AsyncClient) -> None:
    payload = {key: value for key, value in ENDPOINT_PAYLOAD.items() if key != "name"}

    created = await client.post("/api/models", json=payload)

    assert created.status_code == 201
    assert created.json()["name"] == "127.0.0.1:8001"


@pytest.mark.asyncio
async def test_update_edits_fields_and_deactivates_without_touching_the_key(
    client: httpx.AsyncClient,
    database: Database,
) -> None:
    created = (await client.post("/api/models", json=ENDPOINT_PAYLOAD)).json()

    updated = await client.patch(
        f"/api/models/{created['id']}",
        json={"name": "qwen-renamed", "request_timeout": 120, "is_active": False},
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "qwen-renamed"
    assert updated.json()["request_timeout"] == 120
    assert updated.json()["is_active"] is False
    assert updated.json()["has_api_key"] is True
    with database.connect() as connection:
        stored = connection.execute("SELECT encrypted_api_key FROM model_endpoints").fetchone()[0]
    assert stored is not None


@pytest.mark.asyncio
async def test_update_can_rotate_and_clear_the_api_key(client: httpx.AsyncClient) -> None:
    created = (await client.post("/api/models", json=ENDPOINT_PAYLOAD)).json()

    rotated = await client.patch(f"/api/models/{created['id']}", json={"api_key": "rotated"})
    cleared = await client.patch(f"/api/models/{created['id']}", json={"api_key": None})

    assert rotated.json()["has_api_key"] is True
    assert "rotated" not in rotated.text
    assert cleared.json()["has_api_key"] is False


@pytest.mark.asyncio
async def test_probe_reports_success_without_exposing_the_key(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    requests = install_probe_transport(
        api_app,
        lambda _request: httpx.Response(200, json={"data": [{"id": "Qwen3-32B"}]}),
    )
    created = (await client.post("/api/models", json=ENDPOINT_PAYLOAD)).json()

    probe = await client.post(f"/api/models/{created['id']}/test")

    assert probe.status_code == 200
    assert probe.json()["ok"] is True
    assert probe.json().keys() == {"ok", "latency_ms", "message", "models"}
    assert probe.json()["latency_ms"] >= 0
    assert "secret-token" not in probe.text
    assert str(requests[-1].url) == "http://127.0.0.1:8001/v1/models"
    assert requests[-1].headers["authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_probe_reports_unreachable_endpoints_as_a_diagnostic_result(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    install_probe_transport(api_app, refuse)
    created = (await client.post("/api/models", json=ENDPOINT_PAYLOAD)).json()

    probe = await client.post(f"/api/models/{created['id']}/test")

    assert probe.status_code == 200
    assert probe.json()["ok"] is False
    assert probe.json()["message"]


@pytest.mark.asyncio
async def test_probe_reports_rejected_credentials_as_a_diagnostic_result(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    install_probe_transport(api_app, lambda _request: httpx.Response(401, text="unauthorized"))
    created = (await client.post("/api/models", json=ENDPOINT_PAYLOAD)).json()

    probe = await client.post(f"/api/models/{created['id']}/test")

    assert probe.status_code == 200
    assert probe.json()["ok"] is False
    assert "401" in probe.json()["message"]


@pytest.mark.asyncio
async def test_probe_omits_authorization_when_no_key_is_stored(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    requests = install_probe_transport(api_app, lambda _request: httpx.Response(200, json={}))
    payload = {key: value for key, value in ENDPOINT_PAYLOAD.items() if key != "api_key"}
    created = (await client.post("/api/models", json=payload)).json()

    await client.post(f"/api/models/{created['id']}/test")

    assert "authorization" not in requests[-1].headers


@pytest.mark.asyncio
async def test_model_endpoints_require_authentication(
    anonymous_client: httpx.AsyncClient,
) -> None:
    assert (await anonymous_client.get("/api/models")).status_code == 401
    assert (await anonymous_client.post("/api/models", json=ENDPOINT_PAYLOAD)).status_code == 401
    assert (await anonymous_client.get("/api/models/any-id")).status_code == 401
    assert (await anonymous_client.post("/api/models/any-id/test")).status_code == 401


@pytest.mark.asyncio
async def test_unknown_endpoint_is_not_distinguishable_from_another_owners(
    client: httpx.AsyncClient,
) -> None:
    missing = await client.get("/api/models/00000000-0000-0000-0000-000000000000")

    assert missing.status_code == 404
    assert missing.json()["detail"] == "model endpoint not found"

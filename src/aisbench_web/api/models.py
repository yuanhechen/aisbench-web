import time
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr, field_validator

from aisbench_web.dependencies import get_current_user
from aisbench_web.repositories.models import (
    DuplicateEndpointNameError,
    ModelEndpoint,
    ModelEndpointRepository,
)
from aisbench_web.repositories.users import User
from aisbench_web.security import api_key_cipher, load_or_create_secret

router = APIRouter(prefix="/api/models")

NAME_MAX_LENGTH = 128
MODEL_NAME_MAX_LENGTH = 256
API_KEY_MAX_LENGTH = 4096
REQUEST_TIMEOUT_RANGE = (1, 600)
MAX_OUTPUT_LENGTH_RANGE = (1, 131072)
PROBE_TIMEOUT_SECONDS = 5.0
NOT_FOUND_DETAIL = "model endpoint not found"
ADDRESS_FIELDS = frozenset({"host", "port", "use_https"})


def _validated_text(value: str, *, field: str, max_length: int) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be blank")
    if len(stripped) > max_length:
        raise ValueError(f"{field} must contain at most {max_length} characters")
    return stripped


def _validated_host(value: str) -> str:
    """A bare host or IP: no scheme, no port, no path. The port is its own field."""
    host = value.strip()
    if not host:
        raise ValueError("host must not be blank")
    if "://" in host or "/" in host or ":" in host.strip("[]"):
        raise ValueError("host must be a bare hostname or IP address, without scheme or port")
    if len(host) > NAME_MAX_LENGTH:
        raise ValueError(f"host must contain at most {NAME_MAX_LENGTH} characters")
    return host


def base_url_for(host: str, port: int, use_https: bool) -> str:
    """OpenAI-compatible services expose their API under /v1 of the service root."""
    return f"{'https' if use_https else 'http'}://{host}:{port}/v1"


def address_of(base_url: str) -> tuple[str, int, bool]:
    """Recover the address fields a stored base_url was built from, for display and editing."""
    parsed = urlsplit(base_url)
    use_https = parsed.scheme.casefold() == "https"
    return parsed.hostname or "", parsed.port or (443 if use_https else 80), use_https


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("base_url must use http or https")
    if not parsed.hostname:
        raise ValueError("base_url must include a host")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    return value.strip().rstrip("/")


class ModelEndpointCreate(BaseModel):
    """The user says where the service is; what it serves is asked of the service."""

    host: str
    port: int = Field(ge=1, le=65535)
    name: str = ""
    use_https: bool = False
    api_key: SecretStr | None = None
    request_timeout: int = Field(
        default=60, ge=REQUEST_TIMEOUT_RANGE[0], le=REQUEST_TIMEOUT_RANGE[1]
    )
    max_output_length: int = Field(
        default=512, ge=MAX_OUTPUT_LENGTH_RANGE[0], le=MAX_OUTPUT_LENGTH_RANGE[1]
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if value.strip() == "":
            return ""
        return _validated_text(value, field="name", max_length=NAME_MAX_LENGTH)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return _validated_host(value)

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) > API_KEY_MAX_LENGTH:
            raise ValueError(f"api_key must contain at most {API_KEY_MAX_LENGTH} characters")
        return value


class ModelEndpointUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    use_https: bool | None = None
    api_key: SecretStr | None = None
    request_timeout: int | None = Field(
        default=None, ge=REQUEST_TIMEOUT_RANGE[0], le=REQUEST_TIMEOUT_RANGE[1]
    )
    max_output_length: int | None = Field(
        default=None, ge=MAX_OUTPUT_LENGTH_RANGE[0], le=MAX_OUTPUT_LENGTH_RANGE[1]
    )
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validated_text(value, field="name", max_length=NAME_MAX_LENGTH)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str | None) -> str | None:
        return None if value is None else _validated_host(value)


class ModelEndpointResponse(BaseModel):
    id: str
    name: str
    host: str
    port: int
    use_https: bool
    base_url: str
    model_name: str
    has_api_key: bool
    request_timeout: int
    max_output_length: int
    is_active: bool

    @classmethod
    def from_endpoint(cls, endpoint: ModelEndpoint) -> "ModelEndpointResponse":
        host, port, use_https = address_of(endpoint.base_url)
        return cls(
            id=endpoint.id,
            name=endpoint.name,
            host=host,
            port=port,
            use_https=use_https,
            base_url=endpoint.base_url,
            model_name=endpoint.model_name,
            has_api_key=endpoint.has_api_key,
            request_timeout=endpoint.request_timeout,
            max_output_length=endpoint.max_output_length,
            is_active=endpoint.is_active,
        )


class ProbeResponse(BaseModel):
    ok: bool
    latency_ms: int
    message: str
    models: list[str] = []


class ModelEndpointProber:
    """Diagnostic probe against an OpenAI-compatible endpoint's model listing."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def probe(self, base_url: str, api_key: str | None) -> ProbeResponse:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=PROBE_TIMEOUT_SECONDS,
            ) as client:
                response = await client.get(f"{base_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            return ProbeResponse(
                ok=False,
                latency_ms=self._elapsed_ms(started),
                message=f"Could not reach the model API: {exc}",
            )

        latency_ms = self._elapsed_ms(started)
        if response.is_success:
            return ProbeResponse(
                ok=True,
                latency_ms=latency_ms,
                message="Model API reachable",
                models=self._model_ids(response),
            )
        return ProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            message=f"Model API responded with HTTP {response.status_code}",
        )

    @staticmethod
    def _model_ids(response: httpx.Response) -> list[str]:
        """Read the OpenAI listing shape, tolerating a service that answers something else."""
        try:
            payload = response.json()
        except ValueError:
            return []
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        return [
            str(entry["id"])
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((time.perf_counter() - started) * 1000))


def get_model_endpoint_repository(request: Request) -> ModelEndpointRepository:
    return ModelEndpointRepository(request.app.state.database)


def get_api_key_cipher(request: Request) -> Fernet:
    cipher = getattr(request.app.state, "api_key_cipher", None)
    if cipher is None:
        secret = load_or_create_secret(request.app.state.settings.secret_path)
        cipher = api_key_cipher(secret)
        request.app.state.api_key_cipher = cipher
    return cipher


def get_endpoint_prober(request: Request) -> ModelEndpointProber:
    return ModelEndpointProber(transport=getattr(request.app.state, "http_transport", None))


RepositoryDependency = Annotated[ModelEndpointRepository, Depends(get_model_endpoint_repository)]
CipherDependency = Annotated[Fernet, Depends(get_api_key_cipher)]
ProberDependency = Annotated[ModelEndpointProber, Depends(get_endpoint_prober)]
CurrentUserDependency = Annotated[User, Depends(get_current_user)]


def _encrypted(cipher: Fernet, api_key: SecretStr | None) -> bytes | None:
    if api_key is None:
        return None
    return cipher.encrypt(api_key.get_secret_value().encode("utf-8"))


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


@router.post("", response_model=ModelEndpointResponse, status_code=status.HTTP_201_CREATED)
async def create_model_endpoint(
    payload: ModelEndpointCreate,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    cipher: CipherDependency,
    prober: ProberDependency,
) -> ModelEndpointResponse:
    base_url = base_url_for(payload.host, payload.port, payload.use_https)
    api_key = None if payload.api_key is None else payload.api_key.get_secret_value()
    # A temporarily unreachable service must not block saving (design section 7.1), so a failed
    # detection leaves the model name empty; AISBench detects it again at run time.
    detected = await prober.probe(base_url, api_key)
    try:
        endpoint = repository.create(
            owner_id=user.id,
            name=payload.name or f"{payload.host}:{payload.port}",
            base_url=base_url,
            model_name=detected.models[0] if detected.models else "",
            encrypted_api_key=_encrypted(cipher, payload.api_key),
            request_timeout=payload.request_timeout,
            max_output_length=payload.max_output_length,
        )
    except DuplicateEndpointNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a model endpoint with that name already exists",
        ) from exc
    return ModelEndpointResponse.from_endpoint(endpoint)


@router.get("", response_model=list[ModelEndpointResponse])
def list_model_endpoints(
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> list[ModelEndpointResponse]:
    return [
        ModelEndpointResponse.from_endpoint(endpoint)
        for endpoint in repository.list_for_owner(user.id)
    ]


@router.get("/{endpoint_id}", response_model=ModelEndpointResponse)
def get_model_endpoint(
    endpoint_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
) -> ModelEndpointResponse:
    endpoint = repository.get_for_owner(user.id, endpoint_id)
    if endpoint is None:
        raise _not_found()
    return ModelEndpointResponse.from_endpoint(endpoint)


@router.patch("/{endpoint_id}", response_model=ModelEndpointResponse)
def update_model_endpoint(
    endpoint_id: str,
    payload: ModelEndpointUpdate,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    cipher: CipherDependency,
) -> ModelEndpointResponse:
    current = repository.get_for_owner(user.id, endpoint_id)
    if current is None:
        raise _not_found()

    changes: dict[str, Any] = {
        field: getattr(payload, field)
        for field in payload.model_fields_set
        if field not in ADDRESS_FIELDS
        and field != "api_key"
        and getattr(payload, field) is not None
    }
    if ADDRESS_FIELDS & payload.model_fields_set:
        host, port, use_https = address_of(current.base_url)
        changes["base_url"] = base_url_for(
            payload.host if payload.host is not None else host,
            payload.port if payload.port is not None else port,
            payload.use_https if payload.use_https is not None else use_https,
        )
    replace_api_key = "api_key" in payload.model_fields_set
    try:
        endpoint = repository.update_for_owner(
            user.id,
            endpoint_id,
            changes=changes,
            api_key_replacement=_encrypted(cipher, payload.api_key),
            replace_api_key=replace_api_key,
        )
    except DuplicateEndpointNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a model endpoint with that name already exists",
        ) from exc
    if endpoint is None:
        raise _not_found()
    return ModelEndpointResponse.from_endpoint(endpoint)


@router.post("/{endpoint_id}/test", response_model=ProbeResponse)
async def test_model_endpoint(
    endpoint_id: str,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    cipher: CipherDependency,
    prober: ProberDependency,
) -> ProbeResponse:
    endpoint = repository.get_for_owner(user.id, endpoint_id)
    if endpoint is None:
        raise _not_found()
    encrypted_api_key = repository.get_encrypted_api_key_for_owner(user.id, endpoint_id)
    api_key = None if encrypted_api_key is None else cipher.decrypt(encrypted_api_key).decode()
    result = await prober.probe(endpoint.base_url, api_key)
    # Testing the connection is also how a renamed or replaced model is picked up.
    if result.models and result.models[0] != endpoint.model_name:
        repository.update_for_owner(
            user.id, endpoint_id, changes={"model_name": result.models[0]}
        )
    return result

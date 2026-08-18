import time
from typing import Annotated, Any
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, SecretStr, field_validator

from aisbench_web.datasets.catalog import load_model_configs
from aisbench_web.dependencies import get_current_user
from aisbench_web.jobs.config_generator import CHAT_ENDPOINT, aisbench_service_url
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
PROBE_TIMEOUT_SECONDS = 5.0
NOT_FOUND_DETAIL = "model endpoint not found"


def _validated_text(value: str, *, field: str, max_length: int) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be blank")
    if len(stripped) > max_length:
        raise ValueError(f"{field} must contain at most {max_length} characters")
    return stripped


def _default_name_for(base_url: str) -> str:
    """A blank display name falls back to the address rather than being rejected."""
    parsed = urlsplit(base_url)
    return parsed.netloc or base_url


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
    """The user gives an address and a key; what the service serves is asked of the service."""

    base_url: str
    name: str = ""
    api_key: SecretStr | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if value.strip() == "":
            return ""
        return _validated_text(value, field="name", max_length=NAME_MAX_LENGTH)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _validated_base_url(value)

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) > API_KEY_MAX_LENGTH:
            raise ValueError(f"api_key must contain at most {API_KEY_MAX_LENGTH} characters")
        return value


class ModelEndpointUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validated_text(value, field="name", max_length=NAME_MAX_LENGTH)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        return None if value is None else _validated_base_url(value)


class ModelEndpointResponse(BaseModel):
    id: str
    name: str
    base_url: str
    model_name: str
    has_api_key: bool
    is_active: bool

    @classmethod
    def from_endpoint(cls, endpoint: ModelEndpoint) -> "ModelEndpointResponse":
        return cls(
            id=endpoint.id,
            name=endpoint.name,
            base_url=endpoint.base_url,
            model_name=endpoint.model_name,
            has_api_key=endpoint.has_api_key,
            is_active=endpoint.is_active,
        )


class ProbeResponse(BaseModel):
    ok: bool
    latency_ms: int
    message: str
    models: list[str] = []
    #: The URL AISBench will actually call for this endpoint.
    request_url: str = ""
    #: Whether that URL exists. Listing models proves the service is up, not that AISBench
    #: can drive it: the model class appends a fixed v1/chat/completions to the service root.
    runnable: bool = True


def _reason_for(exc: httpx.HTTPError) -> str:
    """Describe a transport failure. A timeout's str() is empty, which reads as no reason."""
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out after {PROBE_TIMEOUT_SECONDS:g}s"
    return str(exc) or exc.__class__.__name__


class ModelEndpointProber:
    """Diagnostic probe against an OpenAI-compatible endpoint's model listing."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def probe(self, base_url: str, api_key: str | None) -> ProbeResponse:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        request_url = urljoin(aisbench_service_url(base_url), CHAT_ENDPOINT)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=PROBE_TIMEOUT_SECONDS,
            ) as client:
                response = await client.get(f"{base_url}/models", headers=headers)
                if not response.is_success:
                    return ProbeResponse(
                        ok=False,
                        latency_ms=self._elapsed_ms(started),
                        message=f"Model API responded with HTTP {response.status_code}",
                        request_url=request_url,
                    )
                models = self._model_ids(response)
                # Listing models only proves the service is up. AISBench appends a fixed
                # v1/chat/completions to the service root, so the path it will really call
                # has to exist too, or every request in every job returns 404.
                runnable = await self._path_exists(client, request_url, headers)
        except httpx.HTTPError as exc:
            return ProbeResponse(
                ok=False,
                latency_ms=self._elapsed_ms(started),
                message=f"Could not reach the model API: {_reason_for(exc)}",
                request_url=request_url,
            )

        latency_ms = self._elapsed_ms(started)
        if runnable:
            return ProbeResponse(
                ok=True,
                latency_ms=latency_ms,
                message="Model API reachable",
                models=models,
                request_url=request_url,
                runnable=True,
            )
        return ProbeResponse(
            ok=False,
            latency_ms=latency_ms,
            message=(
                "The service answered, but AISBench would request "
                f"{request_url}, which this service does not serve. "
                "AISBench drives endpoints whose chat path is <root>/v1/chat/completions."
            ),
            models=models,
            request_url=request_url,
            runnable=False,
        )

    @staticmethod
    async def _path_exists(
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> bool:
        """A GET is enough to tell an existing chat path from a missing one, and costs nothing.

        Anything but 404 means the path is served; a POST would spend tokens to learn the same.
        """
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            return False
        return response.status_code != 404

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


class ModelConfigResponse(BaseModel):
    """An AISBench model config that can drive an HTTP endpoint."""

    name: str
    family: str
    class_name: str
    stream: bool


@router.get("/configs", response_model=list[ModelConfigResponse])
def list_model_configs(_user: CurrentUserDependency) -> list[ModelConfigResponse]:
    """The model classes the installed AISBench ships for API endpoints.

    Which one drives an endpoint is the user's choice, as it is on the command line: they
    are different model classes, not settings of one.
    """
    return [
        ModelConfigResponse(
            name=config.name,
            family=config.family,
            class_name=config.class_name,
            stream=config.stream,
        )
        for config in load_model_configs()
    ]


class ProbeRequest(BaseModel):
    """An address the user has typed but not saved yet."""

    base_url: str
    api_key: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _validated_base_url(value)


@router.post("/probe", response_model=ProbeResponse)
async def probe_address(
    payload: ProbeRequest,
    _user: CurrentUserDependency,
    prober: ProberDependency,
) -> ProbeResponse:
    """Test an address before it is saved and report the models it serves.

    Nothing is stored. Like the saved-endpoint test, an unreachable address is a diagnostic
    result rather than an API failure, so the form can show it inline.
    """
    api_key = None if payload.api_key is None else payload.api_key.get_secret_value()
    return await prober.probe(payload.base_url, api_key)


@router.post("", response_model=ModelEndpointResponse, status_code=status.HTTP_201_CREATED)
async def create_model_endpoint(
    payload: ModelEndpointCreate,
    user: CurrentUserDependency,
    repository: RepositoryDependency,
    cipher: CipherDependency,
    prober: ProberDependency,
) -> ModelEndpointResponse:
    api_key = None if payload.api_key is None else payload.api_key.get_secret_value()
    # A temporarily unreachable service must not block saving (design section 7.1), so a failed
    # detection leaves the model name empty; AISBench detects it again at run time.
    detected = await prober.probe(payload.base_url, api_key)
    try:
        endpoint = repository.create(
            owner_id=user.id,
            name=payload.name or _default_name_for(payload.base_url),
            base_url=payload.base_url,
            model_name=detected.models[0] if detected.models else "",
            encrypted_api_key=_encrypted(cipher, payload.api_key),
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
    changes: dict[str, Any] = {
        field: getattr(payload, field)
        for field in payload.model_fields_set
        if field != "api_key" and getattr(payload, field) is not None
    }
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

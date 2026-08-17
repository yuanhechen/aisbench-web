from contextlib import asynccontextmanager
from ipaddress import AddressValueError, IPv4Address, IPv6Address
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from aisbench_web.api.auth import router as auth_router
from aisbench_web.db import Database
from aisbench_web.settings import Settings

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REG_NAME_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;="
)
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_valid_reg_name(host: str) -> bool:
    if not host:
        return False

    position = 0
    while position < len(host):
        character = host[position]
        if character in REG_NAME_CHARACTERS:
            position += 1
        elif (
            character == "%"
            and position + 2 < len(host)
            and host[position + 1] in HEX_DIGITS
            and host[position + 2] in HEX_DIGITS
        ):
            position += 3
        else:
            return False
    return True


def _is_valid_port_suffix(suffix: str) -> bool:
    if not suffix:
        return True
    port = suffix.removeprefix(":")
    return (
        suffix.startswith(":")
        and 0 < len(port) <= 5
        and port.isascii()
        and port.isdecimal()
        and 0 < int(port) < 65536
    )


def _is_well_formed_authority(authority: str) -> bool:
    if authority.startswith("["):
        closing_bracket = authority.find("]")
        if closing_bracket == -1:
            return False
        ipv6_literal = authority[1:closing_bracket]
        if "%" in ipv6_literal:
            return False
        try:
            IPv6Address(ipv6_literal)
        except AddressValueError:
            return False
        return _is_valid_port_suffix(authority[closing_bracket + 1 :])

    if "[" in authority or "]" in authority:
        return False
    host, separator, port = authority.rpartition(":")
    if not separator:
        host = authority
    elif ":" in host:
        return False

    if not _is_valid_reg_name(host):
        return False
    if "." in host and all(character in "0123456789." for character in host):
        try:
            IPv4Address(host)
        except AddressValueError:
            return False
    return _is_valid_port_suffix(f":{port}" if separator else "")


def _origin_matches_host(origin: str, host: str | None) -> bool:
    if (
        host is None
        or origin.casefold() == "null"
        or "?" in origin
        or "#" in origin
        or any(not 0x21 <= ord(character) <= 0x7E for character in origin)
    ):
        return False
    try:
        parsed = urlsplit(origin)
        parsed_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    if not _is_well_formed_authority(parsed.netloc) or not _is_well_formed_authority(host):
        return False
    if parsed_port is not None and not 0 < parsed_port < 65536:
        return False
    return parsed.netloc.lower() == host.lower()


def create_app(
    *,
    settings: Settings,
    start_worker: bool = True,
) -> FastAPI:
    database = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.migrate()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.start_worker = start_worker
    app.state.database = database

    @app.middleware("http")
    async def require_same_origin_for_api_mutations(request: Request, call_next):
        origins = request.headers.getlist("origin")
        if (
            request.url.path.startswith("/api/")
            and request.method in STATE_CHANGING_METHODS
            and origins
            and (
                len(origins) != 1
                or not _origin_matches_host(origins[0], request.headers.get("host"))
            )
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "origin does not match request host"},
            )
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "aisbench-web"}

    app.include_router(auth_router)

    return app

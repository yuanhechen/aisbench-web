from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from aisbench_web.api.auth import router as auth_router
from aisbench_web.db import Database
from aisbench_web.settings import Settings

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_matches_host(origin: str, host: str | None) -> bool:
    if (
        host is None
        or origin.casefold() == "null"
        or any(character.isspace() for character in origin)
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
    if parsed_port is not None and not 0 < parsed_port < 65536:
        return False
    return parsed.netloc.casefold() == host.casefold()


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
        if (
            request.url.path.startswith("/api/")
            and request.method in STATE_CHANGING_METHODS
            and (origin := request.headers.get("origin")) is not None
            and not _origin_matches_host(origin, request.headers.get("host"))
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

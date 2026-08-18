import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from ipaddress import AddressValueError, IPv4Address, IPv6Address
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

from aisbench_web.api.auth import router as auth_router
from aisbench_web.api.datasets import router as datasets_router
from aisbench_web.api.jobs import router as jobs_router
from aisbench_web.api.models import router as models_router
from aisbench_web.api.results import router as results_router
from aisbench_web.datasets.catalog import CatalogService
from aisbench_web.db import Database
from aisbench_web.jobs.notifier import JobNotifier
from aisbench_web.jobs.worker import Worker, recover_interrupted_jobs
from aisbench_web.repositories.jobs import JobRepository
from aisbench_web.settings import Settings

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REG_NAME_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;="
)
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
DEFAULT_PORTS = {"http": 80, "https": 443}
# Built by scripts/build_frontend.py and shipped inside the wheel.
PACKAGED_STATIC_DIR = Path(__file__).resolve().parent / "static"
API_PREFIXES = ("api", "ws")
# Asset filenames carry a content hash, so a cached copy can never be stale.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
ASSETS_PREFIX = "/assets/"


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


def _parse_authority(authority: str) -> tuple[str, int | None] | None:
    if authority.startswith("["):
        closing_bracket = authority.find("]")
        if closing_bracket == -1:
            return None
        ipv6_literal = authority[1:closing_bracket]
        if "%" in ipv6_literal:
            return None
        try:
            host = str(IPv6Address(ipv6_literal))
        except AddressValueError:
            return None
        suffix = authority[closing_bracket + 1 :]
        if not _is_valid_port_suffix(suffix):
            return None
        return host, int(suffix[1:]) if suffix else None

    if "[" in authority or "]" in authority:
        return None
    host, separator, port = authority.rpartition(":")
    if not separator:
        host = authority
    elif ":" in host:
        return None

    if not _is_valid_reg_name(host):
        return None
    if "." in host and all(character in "0123456789." for character in host):
        try:
            host = str(IPv4Address(host))
        except AddressValueError:
            return None
    suffix = f":{port}" if separator else ""
    if not _is_valid_port_suffix(suffix):
        return None
    return host.lower(), int(port) if separator else None


def _origin_matches_request(origin: str, host: str, request_scheme: str) -> bool:
    request_scheme = request_scheme.lower()
    if (
        request_scheme not in DEFAULT_PORTS
        or origin.casefold() == "null"
        or "?" in origin
        or "#" in origin
        or any(not 0x21 <= ord(character) <= 0x7E for character in origin)
    ):
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() not in DEFAULT_PORTS
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    origin_authority = _parse_authority(parsed.netloc)
    request_authority = _parse_authority(host)
    if origin_authority is None or request_authority is None:
        return False

    origin_host, origin_port = origin_authority
    request_host, request_port = request_authority
    origin_scheme = parsed.scheme.lower()
    return (
        origin_scheme,
        origin_host,
        origin_port or DEFAULT_PORTS[origin_scheme],
    ) == (
        request_scheme,
        request_host,
        request_port or DEFAULT_PORTS[request_scheme],
    )


def create_app(
    *,
    settings: Settings,
    start_worker: bool = True,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the service. `static_dir` overrides the packaged interface, for tests."""
    web_root = PACKAGED_STATIC_DIR if static_dir is None else Path(static_dir)
    database = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.migrate()
        CatalogService(database, settings).sync()
        # Anything left claimed belongs to a process that is gone; queued work keeps its place.
        recover_interrupted_jobs(JobRepository(database))
        # One shared slot: concurrent downloads of the same catalog would race for disk and
        # bandwidth, and the dataset lock already serializes per dataset.
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dataset-install")
        app.state.install_executor = executor
        app.state.install_tasks = []
        app.state.notifier.bind_loop(asyncio.get_running_loop())
        worker = (
            Worker(database, settings, notifier=app.state.notifier) if start_worker else None
        )
        app.state.worker = worker
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            # Stop claiming and terminate this worker's process groups before the executor
            # goes away, so no job is left running with nothing tracking it.
            if worker is not None:
                worker.stop()
            executor.shutdown(wait=True)

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.start_worker = start_worker
    app.state.database = database
    app.state.install_executor = None
    app.state.install_tasks: list[Future] = []
    app.state.worker = None
    app.state.notifier = JobNotifier()
    app.state.static_dir = web_root

    @app.exception_handler(RequestValidationError)
    async def validation_error_without_raw_inputs(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = [
            {key: value for key, value in error.items() if key != "input"} for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(errors)},
        )

    # The interface is often served over a slow link; the bundle compresses about three to
    # one, which is the difference between a fast page and a page that feels stuck.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def cache_hashed_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(ASSETS_PREFIX) and response.status_code == 200:
            response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
        return response

    @app.middleware("http")
    async def require_same_origin_for_api_mutations(request: Request, call_next):
        origins = request.headers.getlist("origin")
        hosts = request.headers.getlist("host")
        if (
            request.url.path.startswith("/api/")
            and request.method in STATE_CHANGING_METHODS
            and origins
            and (
                len(origins) != 1
                or len(hosts) != 1
                or not _origin_matches_request(origins[0], hosts[0], request.url.scheme)
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
    app.include_router(models_router)
    app.include_router(datasets_router)
    app.include_router(results_router)
    app.include_router(jobs_router)

    assets_dir = web_root / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.api_route("/{spa_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def serve_single_page_application(spa_path: str, request: Request) -> FileResponse:
        """Serve the app for browser routes; an unknown API path stays a JSON 404."""
        first_segment = spa_path.split("/", 1)[0]
        if first_segment in API_PREFIXES:
            raise HTTPException(status_code=404, detail="Not Found")
        index = Path(request.app.state.static_dir) / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=404,
                detail="the web interface is not included in this build",
            )
        return FileResponse(index, media_type="text/html")

    return app

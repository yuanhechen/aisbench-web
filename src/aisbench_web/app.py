from contextlib import asynccontextmanager

from fastapi import FastAPI

from aisbench_web.db import Database
from aisbench_web.settings import Settings


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

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "aisbench-web"}

    return app

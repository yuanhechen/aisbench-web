from fastapi import FastAPI

from aisbench_web.settings import Settings


def create_app(
    *,
    settings: Settings,
    start_worker: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.start_worker = start_worker

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "aisbench-web"}

    return app

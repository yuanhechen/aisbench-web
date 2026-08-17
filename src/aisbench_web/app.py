from pathlib import Path

from fastapi import FastAPI


def create_app(
    *,
    data_dir: Path,
    max_concurrent_jobs: int = 1,
    start_worker: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.state.data_dir = data_dir
    app.state.max_concurrent_jobs = max_concurrent_jobs
    app.state.start_worker = start_worker

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "aisbench-web"}

    return app

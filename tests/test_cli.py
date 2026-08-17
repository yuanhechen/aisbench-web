import sys
from pathlib import Path

import httpx
import pytest

from aisbench_web import cli
from aisbench_web.app import create_app
from aisbench_web.cli import parse_args


def test_parse_args_uses_simple_server_defaults():
    args = parse_args([])
    assert args.host == "0.0.0.0"
    assert args.port == 8000
    assert args.data_dir == Path.home() / ".aisbench-web"
    assert args.max_concurrent_jobs == 1


@pytest.mark.asyncio
async def test_health_endpoint_reports_service_name(tmp_path):
    app = create_app(data_dir=tmp_path, start_worker=False)
    assert app.state.data_dir == tmp_path
    assert app.state.max_concurrent_jobs == 1
    assert app.state.start_worker is False

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "aisbench-web"}


def test_main_runs_uvicorn_with_parsed_server_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aisbench-web",
            "--host",
            "127.0.0.1",
            "--port",
            "9001",
            "--data-dir",
            str(tmp_path),
            "--max-concurrent-jobs",
            "3",
        ],
    )
    run_call = {}

    def fake_run(app, *, host, port):
        run_call.update(app=app, host=host, port=port)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    cli.main()

    assert run_call["host"] == "127.0.0.1"
    assert run_call["port"] == 9001
    app = run_call["app"]
    assert app.state.data_dir == tmp_path
    assert app.state.max_concurrent_jobs == 3
    assert app.state.start_worker is True

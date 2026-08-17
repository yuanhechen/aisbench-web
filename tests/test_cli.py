import logging
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import httpx
import pytest

from aisbench_web import cli
from aisbench_web.app import create_app
from aisbench_web.cli import parse_args
from aisbench_web.settings import Settings


def test_parse_args_uses_simple_server_defaults():
    args = parse_args([])
    assert args.host == "0.0.0.0"
    assert args.port == 8000
    assert args.data_dir == Path.home() / ".aisbench-web"
    assert args.max_concurrent_jobs == 1


@pytest.mark.asyncio
async def test_health_endpoint_reports_service_name(tmp_path):
    settings = Settings.create(tmp_path, tmp_path / "ais_bench", 1)
    app = create_app(settings=settings, start_worker=False)
    assert app.state.settings is settings
    assert app.state.start_worker is False

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "aisbench-web"}


def test_main_runs_uvicorn_with_runtime_configuration(tmp_path, monkeypatch, caplog):
    executable = (tmp_path / "ais_bench").resolve()
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
    probe_call = {}
    version_call = {}

    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)

    def fake_probe(command, **kwargs):
        probe_call.update(command=command, kwargs=kwargs)
        return subprocess.CompletedProcess(command, 0)

    def fake_version(package_name):
        version_call["package_name"] = package_name
        return "1.2.3"

    def fake_run(app, *, host, port):
        run_call.update(app=app, host=host, port=port)

    monkeypatch.setattr(cli.subprocess, "run", fake_probe)
    monkeypatch.setattr(cli.metadata, "version", fake_version)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    caplog.set_level(logging.INFO)

    cli.main()

    assert probe_call["command"] == [str(executable), "--help"]
    assert probe_call["kwargs"]["timeout"] == 15
    assert version_call["package_name"] == "ais_bench_benchmark"
    assert run_call["host"] == "127.0.0.1"
    assert run_call["port"] == 9001
    app = run_call["app"]
    assert isinstance(app.state.settings, Settings)
    assert app.state.settings.data_dir == tmp_path.resolve()
    assert app.state.settings.ais_bench_path == executable
    assert app.state.settings.max_concurrent_jobs == 3
    assert app.state.start_worker is True
    assert str(executable) in caplog.text
    assert (
        "AISBench package version in the aisbench-web Python environment: 1.2.3"
        in caplog.text
    )


def test_main_reports_probe_timeout_distinctly(tmp_path, monkeypatch):
    executable = (tmp_path / "ais_bench").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        ["aisbench-web", "--data-dir", str(tmp_path), "--host", "localhost"],
    )
    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)

    def fail_probe(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(cli.subprocess, "run", fail_probe)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("Uvicorn must not start after a failed probe"),
    )

    with pytest.raises(RuntimeError, match="AISBench probe timed out after 15 seconds"):
        cli.main()


def test_main_reports_probe_launch_failure_distinctly(tmp_path, monkeypatch):
    executable = (tmp_path / "ais_bench").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        ["aisbench-web", "--data-dir", str(tmp_path), "--host", "localhost"],
    )
    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)

    def fail_probe(command, **kwargs):
        raise OSError("bad executable format")

    monkeypatch.setattr(cli.subprocess, "run", fail_probe)

    with pytest.raises(RuntimeError, match="Could not launch AISBench executable"):
        cli.main()


def test_main_surfaces_bounded_probe_stderr(tmp_path, monkeypatch):
    executable = (tmp_path / "ais_bench").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        ["aisbench-web", "--data-dir", str(tmp_path), "--host", "localhost"],
    )
    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)
    diagnostic = "invalid benchmark arguments: " + ("detail " * 2_000) + "UNBOUNDED_TAIL"

    def fail_probe(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=2,
            cmd=command,
            output="stdout must not replace stderr",
            stderr=f"  {diagnostic}  \n",
        )

    monkeypatch.setattr(cli.subprocess, "run", fail_probe)

    with pytest.raises(RuntimeError) as exc_info:
        cli.main()

    message = str(exc_info.value)
    assert "AISBench probe exited with status 2" in message
    assert "invalid benchmark arguments" in message
    assert "stdout must not replace stderr" not in message
    assert "UNBOUNDED_TAIL" not in message
    assert len(message) < 1_000


def test_main_uses_probe_stdout_when_stderr_is_blank(tmp_path, monkeypatch):
    executable = (tmp_path / "ais_bench").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        ["aisbench-web", "--data-dir", str(tmp_path), "--host", "localhost"],
    )
    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)

    def fail_probe(command, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=2,
            cmd=command,
            output="  stdout diagnostic  \n",
            stderr=" \n",
        )

    monkeypatch.setattr(cli.subprocess, "run", fail_probe)

    with pytest.raises(RuntimeError, match="stdout diagnostic"):
        cli.main()


def test_main_warns_when_aisbench_version_is_unknown(tmp_path, monkeypatch, caplog):
    executable = (tmp_path / "ais_bench").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        ["aisbench-web", "--data-dir", str(tmp_path), "--host", "localhost"],
    )
    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    def unknown_version(package_name):
        assert package_name == "ais_bench_benchmark"
        raise metadata.PackageNotFoundError(package_name)

    run_call = {}
    monkeypatch.setattr(cli.metadata, "version", unknown_version)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: run_call.update(app=app))
    caplog.set_level(logging.WARNING)

    cli.main()

    assert (
        "AISBench package version in the aisbench-web Python environment is unknown"
        in caplog.text
    )
    assert "app" in run_call


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10"])
def test_main_warns_when_listening_beyond_loopback(tmp_path, monkeypatch, caplog, host):
    executable = (tmp_path / "ais_bench").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        ["aisbench-web", "--data-dir", str(tmp_path), "--host", host],
    )
    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(cli.metadata, "version", lambda package_name: "1.2.3")
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: None)
    caplog.set_level(logging.WARNING)

    cli.main()

    assert "trusted network" in caplog.text


@pytest.mark.parametrize("host", ["127.0.0.2", "::1", "LOCALHOST"])
def test_main_does_not_warn_for_loopback_host(tmp_path, monkeypatch, caplog, host):
    executable = (tmp_path / "ais_bench").resolve()
    monkeypatch.setattr(
        sys,
        "argv",
        ["aisbench-web", "--data-dir", str(tmp_path), "--host", host],
    )
    monkeypatch.setattr(cli, "discover_ais_bench", lambda: executable)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(cli.metadata, "version", lambda package_name: "1.2.3")
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: None)
    caplog.set_level(logging.WARNING)

    cli.main()

    assert "trusted network" not in caplog.text

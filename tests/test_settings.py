import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from aisbench_web.settings import Settings, discover_ais_bench


def test_discover_ais_bench_prefers_current_environment(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "ais_bench"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.delenv("AISBENCH_WEB_AIS_BENCH_PATH", raising=False)
    monkeypatch.setenv("PATH", str(executable.parent))
    assert discover_ais_bench() == executable


def test_discover_ais_bench_accepts_explicit_test_override(tmp_path, monkeypatch):
    executable = tmp_path / "fake_ais_bench"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("AISBENCH_WEB_AIS_BENCH_PATH", str(executable))
    assert discover_ais_bench() == executable.resolve()


@pytest.mark.parametrize("candidate_kind", ["missing", "directory", "not-executable"])
def test_discover_ais_bench_rejects_invalid_explicit_override(
    tmp_path, monkeypatch, candidate_kind
):
    candidate = tmp_path / "invalid_ais_bench"
    if candidate_kind == "directory":
        candidate.mkdir()
    elif candidate_kind == "not-executable":
        candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        candidate.chmod(0o644)

    monkeypatch.setenv("AISBENCH_WEB_AIS_BENCH_PATH", str(candidate))

    with pytest.raises(
        RuntimeError,
        match="AISBENCH_WEB_AIS_BENCH_PATH is not an executable file",
    ):
        discover_ais_bench()


def test_discover_ais_bench_explains_when_command_is_unavailable(monkeypatch):
    monkeypatch.delenv("AISBENCH_WEB_AIS_BENCH_PATH", raising=False)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(RuntimeError, match="activate or install AISBench"):
        discover_ais_bench()


def test_settings_create_private_layout(tmp_path):
    settings = Settings.create(tmp_path, tmp_path / "ais_bench", 1)
    settings.ensure_layout()
    assert settings.db_path == tmp_path / "aisbench-web.db"
    assert settings.secret_path == tmp_path / "secret.key"
    assert settings.jobs_dir.is_dir()
    assert settings.downloads_dir.is_dir()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_settings_data_directory_is_private(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    settings = Settings.create(data_dir, tmp_path / "ais_bench", 1)

    settings.ensure_layout()

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


def test_settings_create_normalizes_paths_and_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings.create(Path("~/data"), Path("~/bin/ais_bench"), 1)

    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.ais_bench_path == (tmp_path / "bin" / "ais_bench").resolve()
    with pytest.raises(FrozenInstanceError):
        settings.max_concurrent_jobs = 2


def test_invalid_concurrency_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="at least 1"):
        Settings.create(tmp_path, Path("/bin/true"), 0)

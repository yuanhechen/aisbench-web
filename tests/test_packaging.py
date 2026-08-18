import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from fastapi import FastAPI

from aisbench_web.app import PACKAGED_STATIC_DIR, create_app
from aisbench_web.datasets.catalog import DOWNLOADS_RESOURCE

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
INDEX_MARKER = '<div id="root"></div>'


@pytest.fixture
def built_app(settings, tmp_path: Path) -> FastAPI:
    """Build the app against a stand-in interface, so static serving is exercised for real."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        f"<!doctype html><html><body>{INDEX_MARKER}</body></html>", encoding="utf-8"
    )
    (static / "assets" / "index.js").write_text(
        "const compressible = 'x';\n" * 400, encoding="utf-8"
    )
    return create_app(settings=settings, start_worker=False, static_dir=static)


@pytest.mark.asyncio
async def test_spa_fallback_serves_index(built_app: FastAPI) -> None:
    async with (
        built_app.router.lifespan_context(built_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=built_app),
            base_url="http://testserver",
        ) as client,
    ):
        for path in ("/", "/jobs/job-1", "/models", "/comparison"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert INDEX_MARKER in response.text


@pytest.mark.asyncio
async def test_unknown_api_paths_stay_json_and_never_return_the_app(built_app: FastAPI) -> None:
    async with (
        built_app.router.lifespan_context(built_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=built_app),
            base_url="http://testserver",
        ) as client,
    ):
        for path in ("/api/nope", "/api/jobs/unknown/nope", "/ws/nope"):
            response = await client.get(path)
            assert response.status_code == 404, path
            assert INDEX_MARKER not in response.text
            assert response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_the_interface_is_compressed_and_its_assets_are_cacheable(
    built_app: FastAPI,
) -> None:
    """Served over a slow link, an uncompressed bundle is the difference users feel."""
    async with (
        built_app.router.lifespan_context(built_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=built_app),
            base_url="http://testserver",
        ) as client,
    ):
        response = await client.get("/assets/index.js", headers={"Accept-Encoding": "gzip"})
        head = await client.head("/jobs/new")

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    # Hashed filenames cannot go stale, so a cached copy needs no revalidation.
    assert "immutable" in response.headers["cache-control"]
    # A HEAD on a browser route used to be refused with 405.
    assert head.status_code == 200


@pytest.mark.asyncio
async def test_a_build_without_the_interface_says_so_instead_of_500(api_app: FastAPI) -> None:
    api_app.state.static_dir = Path("/nonexistent-static")
    async with (
        api_app.router.lifespan_context(api_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app),
            base_url="http://testserver",
        ) as client,
    ):
        response = await client.get("/")

    assert response.status_code == 404
    assert "web interface" in response.json()["detail"]


def test_build_script_refuses_to_delete_anything_but_the_packaged_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_frontend = _load_build_script()
    monkeypatch.setattr(build_frontend, "PACKAGE_STATIC", Path("/tmp/somewhere-else"))

    with pytest.raises(SystemExit, match="not the packaged static directory"):
        build_frontend._validated_static_dir()


def _load_build_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_frontend", REPOSITORY_ROOT / "scripts" / "build_frontend.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not (PACKAGED_STATIC_DIR / "index.html").is_file(),
    reason="run scripts/build_frontend.py first; the wheel check needs a built interface",
)
def test_the_wheel_carries_the_interface_and_the_catalog(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("aisbench_web-*.whl"))

    with ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "aisbench_web/static/index.html" in names
    # Named from the constant the loader uses, so renaming the resource without
    # repackaging it fails here rather than at startup on a user's machine.
    assert f"aisbench_web/datasets/{DOWNLOADS_RESOURCE}" in names
    assert any(name.startswith("aisbench_web/static/assets/") for name in names)
    # The target machine must not need Node; the wheel carries the built output only.
    assert not any("node_modules" in name for name in names)

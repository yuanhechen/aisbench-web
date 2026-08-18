import io
import os
import tarfile
import time
import zipfile
from concurrent.futures import wait
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from aisbench_web.app import create_app
from aisbench_web.datasets.catalog import CatalogService, load_catalog, resolve_datasets_root
from aisbench_web.datasets.installer import DatasetInstaller, extract_archive
from aisbench_web.db import Database
from aisbench_web.repositories.datasets import DatasetRepository
from aisbench_web.settings import Settings
from conftest import ClientFactory


@pytest.fixture
def datasets_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "site-packages" / "ais_bench" / "datasets"
    root.mkdir(parents=True)
    monkeypatch.setenv("AISBENCH_DATASETS_DIR", str(root))
    return root


@pytest.fixture
def api_app(settings: Settings, datasets_root: Path) -> FastAPI:
    return create_app(settings=settings, start_worker=False)


@pytest.fixture
def catalog_service(database: Database, settings: Settings, datasets_root: Path) -> CatalogService:
    database.migrate()
    return CatalogService(database, settings)


class _FakeUsage:
    def __init__(self, free: int) -> None:
        self.free = free
        self.total = free
        self.used = 0


def build_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# --- catalog -----------------------------------------------------------------


def test_packaged_catalog_declares_every_field_the_service_needs() -> None:
    entries = load_catalog()

    assert {entry.id for entry in entries} >= {"gsm8k", "ceval", "mmlu"}
    for entry in entries:
        assert entry.name and entry.description and entry.relative_data_path
        assert entry.accuracy_config
        assert not Path(entry.relative_data_path).is_absolute()
        assert ".." not in Path(entry.relative_data_path).parts


def test_catalog_matches_the_verified_aisbench_layout() -> None:
    by_id = {entry.id: entry for entry in load_catalog()}

    assert by_id["gsm8k"].accuracy_config == "gsm8k_gen_4_shot_cot_chat_prompt"
    assert by_id["gsm8k"].performance_config == "gsm8k_gen_0_shot_cot_str_perf"
    assert by_id["ceval"].relative_data_path == "ceval/formal_ceval"
    # The installed AISBench ships no mmlu *_perf config, so performance must stay unavailable.
    assert by_id["mmlu"].performance_config is None


def test_datasets_root_prefers_the_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISBENCH_DATASETS_DIR", str(tmp_path))

    assert resolve_datasets_root() == tmp_path.resolve()

    monkeypatch.delenv("AISBENCH_DATASETS_DIR")
    # ais_bench is not installed in the test environment, so resolution degrades instead of raising.
    assert resolve_datasets_root() is None


@pytest.mark.asyncio
async def test_catalog_is_shared_between_users(client_factory: ClientFactory) -> None:
    alice = await client_factory("alice")
    bob = await client_factory("bob")

    alice_view = await alice.get("/api/datasets")
    bob_view = await bob.get("/api/datasets")

    assert alice_view.status_code == 200
    assert alice_view.json() == bob_view.json()
    assert [dataset["id"] for dataset in alice_view.json()] == sorted(
        dataset["id"] for dataset in alice_view.json()
    )


def test_sync_marks_a_dataset_available_when_its_expected_path_exists(
    catalog_service: CatalogService,
    datasets_root: Path,
    database: Database,
) -> None:
    (datasets_root / "gsm8k").mkdir()

    catalog_service.sync()

    by_id = {dataset.id: dataset for dataset in DatasetRepository(database).list_all()}
    assert by_id["gsm8k"].status == "available"
    assert by_id["gsm8k"].local_path == str(datasets_root / "gsm8k")
    assert by_id["mmlu"].status == "not_installed"
    assert by_id["mmlu"].local_path is None


def test_sync_reports_unlisted_directories_as_detected(
    catalog_service: CatalogService,
    datasets_root: Path,
    database: Database,
) -> None:
    (datasets_root / "synthetic").mkdir()

    catalog_service.sync()

    detected = {
        dataset.id: dataset
        for dataset in DatasetRepository(database).list_all()
        if dataset.status == "detected"
    }
    assert set(detected) == {"synthetic"}
    assert detected["synthetic"].can_install is False


def test_sync_clears_installing_left_behind_by_a_previous_process(
    catalog_service: CatalogService,
    database: Database,
) -> None:
    catalog_service.sync()
    repository = DatasetRepository(database)
    assert repository.acquire_install_lock("gsm8k") is True

    catalog_service.sync()

    assert repository.get("gsm8k").status == "not_installed"
    assert repository.acquire_install_lock("gsm8k") is True


def test_sync_removes_only_stale_part_files(
    catalog_service: CatalogService,
    settings: Settings,
) -> None:
    stale = settings.downloads_dir / "gsm8k.part"
    fresh = settings.downloads_dir / "mmlu.part"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    old_enough = time.time() - 25 * 60 * 60
    os.utime(stale, (old_enough, old_enough))

    catalog_service.sync()

    assert not stale.exists()
    assert fresh.exists()


def test_only_one_install_can_hold_dataset_lock(
    catalog_service: CatalogService,
    database: Database,
) -> None:
    catalog_service.sync()
    repository = DatasetRepository(database)

    assert repository.acquire_install_lock("gsm8k") is True
    assert repository.acquire_install_lock("gsm8k") is False


# --- archive safety ----------------------------------------------------------


def test_zip_slip_member_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    archive.write_bytes(build_zip({"../escape.txt": "bad"}))

    with pytest.raises(ValueError, match="unsafe archive member"):
        extract_archive(archive, tmp_path / "target")

    assert not (tmp_path / "escape.txt").exists()


def test_absolute_zip_member_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    archive.write_bytes(build_zip({"/etc/passwd": "bad"}))

    with pytest.raises(ValueError, match="unsafe archive member"):
        extract_archive(archive, tmp_path / "target")


def test_backslash_member_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    archive.write_bytes(build_zip({"..\\escape.txt": "bad"}))

    with pytest.raises(ValueError, match="unsafe archive member"):
        extract_archive(archive, tmp_path / "target")


def test_tar_link_members_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as bundle:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        bundle.addfile(link)

    with pytest.raises(ValueError, match="unsafe archive member"):
        extract_archive(archive, tmp_path / "target")


def test_safe_archive_extracts(tmp_path: Path) -> None:
    archive = tmp_path / "good.zip"
    archive.write_bytes(build_zip({"gsm8k/test.jsonl": '{"q": 1}'}))
    target = tmp_path / "target"

    extract_archive(archive, target)

    assert (target / "gsm8k" / "test.jsonl").read_text() == '{"q": 1}'


# --- installer ---------------------------------------------------------------


def test_install_downloads_extracts_and_renames_atomically(
    tmp_path: Path,
    settings: Settings,
    datasets_root: Path,
) -> None:
    payload = build_zip({"gsm8k/test.jsonl": '{"q": 1}'})
    observed: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(200, content=payload)

    installer = DatasetInstaller(settings.downloads_dir, transport=httpx.MockTransport(serve))
    entry = next(entry for entry in load_catalog() if entry.id == "gsm8k")

    installed = installer.install(entry, datasets_root / entry.relative_data_path)

    assert installed == datasets_root / "gsm8k"
    assert (installed / "test.jsonl").read_text() == '{"q": 1}'
    assert observed == [entry.download_url]
    assert list(settings.downloads_dir.iterdir()) == []
    assert [path.name for path in datasets_root.iterdir()] == ["gsm8k"]


def test_install_rejects_a_checksum_mismatch_and_leaves_nothing_behind(
    settings: Settings,
    datasets_root: Path,
) -> None:
    payload = build_zip({"gsm8k/test.jsonl": "{}"})
    installer = DatasetInstaller(
        settings.downloads_dir,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=payload)),
    )
    entry = next(entry for entry in load_catalog() if entry.id == "gsm8k")
    entry = entry.replace(sha256="0" * 64)

    with pytest.raises(ValueError, match="checksum"):
        installer.install(entry, datasets_root / entry.relative_data_path)

    assert list(datasets_root.iterdir()) == []
    assert list(settings.downloads_dir.iterdir()) == []


def test_install_fails_early_when_the_disk_cannot_hold_the_dataset(
    settings: Settings,
    datasets_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aisbench_web.datasets.installer.shutil.disk_usage",
        lambda _path: _FakeUsage(free=1),
    )
    requested: list[str] = []
    installer = DatasetInstaller(
        settings.downloads_dir,
        transport=httpx.MockTransport(
            lambda request: requested.append(str(request.url)) or httpx.Response(200, content=b"")
        ),
    )
    entry = next(entry for entry in load_catalog() if entry.id == "gsm8k")
    entry = entry.replace(size_bytes=10_000_000)

    with pytest.raises(ValueError, match="disk space"):
        installer.install(entry, datasets_root / entry.relative_data_path)

    assert requested == []


def test_install_rejects_an_unsafe_archive_without_touching_the_target(
    settings: Settings,
    datasets_root: Path,
) -> None:
    payload = build_zip({"../escape.txt": "bad"})
    installer = DatasetInstaller(
        settings.downloads_dir,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=payload)),
    )
    entry = next(entry for entry in load_catalog() if entry.id == "gsm8k")

    with pytest.raises(ValueError, match="unsafe archive member"):
        installer.install(entry, datasets_root / entry.relative_data_path)

    assert list(datasets_root.iterdir()) == []


# --- API ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_returns_202_and_reports_progress_through_the_shared_row(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    datasets_root: Path,
) -> None:
    payload = build_zip({"gsm8k/test.jsonl": "{}"})
    api_app.state.http_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=payload)
    )

    accepted = await client.post("/api/datasets/gsm8k/install")

    assert accepted.status_code == 202
    assert accepted.json()["status"] == "installing"
    wait(api_app.state.install_tasks, timeout=10)
    listed = {dataset["id"]: dataset for dataset in (await client.get("/api/datasets")).json()}
    assert listed["gsm8k"]["status"] == "available"
    assert listed["gsm8k"]["local_path"] == str(datasets_root / "gsm8k")


@pytest.mark.asyncio
async def test_a_failed_install_is_recorded_with_a_retryable_state(
    api_app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    api_app.state.http_transport = httpx.MockTransport(refuse)

    await client.post("/api/datasets/gsm8k/install")
    wait(api_app.state.install_tasks, timeout=10)

    listed = {dataset["id"]: dataset for dataset in (await client.get("/api/datasets")).json()}
    assert listed["gsm8k"]["status"] == "failed"
    assert listed["gsm8k"]["error_message"]
    assert (await client.post("/api/datasets/gsm8k/install")).status_code == 202


@pytest.mark.asyncio
async def test_install_is_refused_for_datasets_without_a_reliable_source(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/datasets/mmlu/install")

    assert response.status_code == 409
    assert "install" in response.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_dataset_returns_404(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/datasets/nope/install")).status_code == 404


@pytest.mark.asyncio
async def test_datasets_require_authentication(anonymous_client: httpx.AsyncClient) -> None:
    assert (await anonymous_client.get("/api/datasets")).status_code == 401
    assert (await anonymous_client.post("/api/datasets/gsm8k/install")).status_code == 401


@pytest.mark.asyncio
async def test_shared_datasets_cannot_be_deleted_through_the_api(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.delete("/api/datasets/gsm8k")).status_code == 405

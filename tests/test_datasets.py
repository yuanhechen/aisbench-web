import hashlib
import io
import os
import shutil
import tarfile
import time
import zipfile
from concurrent.futures import wait
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from aisbench_web.app import create_app
from aisbench_web.datasets.catalog import (
    CatalogEntry,
    CatalogService,
    DownloadSource,
    load_catalog,
    load_download_sources,
    resolve_datasets_root,
)
from aisbench_web.datasets.installer import DatasetInstaller, extract_archive
from aisbench_web.db import Database
from aisbench_web.repositories.datasets import DatasetRepository
from aisbench_web.settings import Settings
from conftest import ClientFactory


@pytest.fixture
def datasets_root(
    aisbench_configs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = tmp_path / "installed-datasets"
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


def gsm8k_entry_for(payload: bytes) -> CatalogEntry:
    """Repin the GSM8K entry at a synthetic payload so verification still runs."""
    entry = next(entry for entry in load_catalog() if entry.id == "gsm8k")
    return CatalogEntry(
        id=entry.id,
        install_path=entry.install_path,
        required_path=entry.required_path,
        configs=entry.configs,
        download=DownloadSource(
            url=entry.download.url,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        ),
    )


def build_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


# --- catalog -----------------------------------------------------------------


def test_catalog_is_read_from_the_installed_aisbench(aisbench_configs: Path) -> None:
    """The installed AISBench is the authority on which datasets and variants exist."""
    entries = {entry.id: entry for entry in load_catalog()}

    assert set(entries) == {"gsm8k", "mmlu", "synthetic"}
    for entry in entries.values():
        assert entry.configs
        for config in entry.configs:
            assert config.symbol.endswith("_datasets")
            assert config.import_path.startswith("ais_bench.benchmark.configs.datasets.")
        assert not Path(entry.install_path).is_absolute()
        assert ".." not in Path(entry.install_path).parts


def test_catalog_separates_accuracy_and_performance_variants(aisbench_configs: Path) -> None:
    entries = {entry.id: entry for entry in load_catalog()}

    gsm8k = entries["gsm8k"]
    # gsm8k_gen aliases gsm8k_gen_4_shot_cot_chat_prompt, so it is folded into it rather
    # than offered as a second way to run the same thing.
    assert [config.name for config in gsm8k.configs_for("accuracy")] == [
        "gsm8k_gen_0_shot_cot_str",
        "gsm8k_gen_4_shot_cot_chat_prompt",
        "gsm8k_ppl_0_shot_str",
    ]
    assert gsm8k.default_config_name("accuracy") == "gsm8k_gen_4_shot_cot_chat_prompt"
    assert [config.name for config in gsm8k.configs_for("performance")] == [
        "gsm8k_gen_0_shot_cot_str_perf"
    ]
    # A dataset AISBench ships no performance config for offers none.
    assert entries["mmlu"].configs_for("performance") == ()


def test_config_names_are_read_for_the_options_they_encode(aisbench_configs: Path) -> None:
    entries = {entry.id: entry for entry in load_catalog()}
    by_name = {config.name: config for config in entries["gsm8k"].configs}

    four_shot = by_name["gsm8k_gen_4_shot_cot_chat_prompt"]
    assert (four_shot.shots, four_shot.chain_of_thought, four_shot.chat_prompt) == (4, True, True)
    zero_shot = by_name["gsm8k_gen_0_shot_cot_str"]
    assert (zero_shot.shots, zero_shot.chain_of_thought, zero_shot.chat_prompt) == (0, True, False)


def test_every_download_source_is_checksum_pinned() -> None:
    """An unpinned download is a silent swap; an unverified target installs into nowhere."""
    for directory, source in load_download_sources().items():
        assert len(source.sha256) == 64
        assert source.size_bytes > 0
        assert source.url.endswith(".zip")
        assert directory and "/" not in directory


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


def test_a_dataset_aisbench_no_longer_ships_is_forgotten(
    catalog_service: CatalogService,
    database: Database,
    aisbench_configs: Path,
) -> None:
    """The catalog follows the installation; a stale row would offer a job that cannot run."""
    catalog_service.sync()
    assert {d.id for d in DatasetRepository(database).list_all()} == {
        "gsm8k",
        "mmlu",
        "synthetic",
    }

    shutil.rmtree(aisbench_configs / "benchmark" / "configs" / "datasets" / "mmlu")
    catalog_service.sync()

    assert {d.id for d in DatasetRepository(database).list_all()} == {"gsm8k", "synthetic"}


def test_only_a_dataset_with_a_verified_source_can_install(
    catalog_service: CatalogService,
    database: Database,
) -> None:
    catalog_service.sync()

    by_id = {dataset.id: dataset for dataset in DatasetRepository(database).list_all()}
    assert by_id["gsm8k"].can_install is True
    # No archive is known to unpack where synthetic's configs read.
    assert by_id["synthetic"].can_install is False


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
    entry = gsm8k_entry_for(payload)

    installed = installer.install(entry, datasets_root / entry.install_path)

    assert installed == datasets_root / "gsm8k"
    assert (installed / "test.jsonl").read_text() == '{"q": 1}'
    assert observed == [entry.download.url]
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
    entry = CatalogEntry(
        id=entry.id,
        install_path=entry.install_path,
        required_path=entry.required_path,
        configs=entry.configs,
        download=DownloadSource(url=entry.download.url, sha256="0" * 64, size_bytes=1),
    )

    with pytest.raises(ValueError, match="checksum"):
        installer.install(entry, datasets_root / entry.install_path)

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
    entry = CatalogEntry(
        id=entry.id,
        install_path=entry.install_path,
        required_path=entry.required_path,
        configs=entry.configs,
        download=DownloadSource(
            url=entry.download.url, sha256=entry.download.sha256, size_bytes=10_000_000
        ),
    )

    with pytest.raises(ValueError, match="disk space"):
        installer.install(entry, datasets_root / entry.install_path)

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
    entry = gsm8k_entry_for(payload)

    with pytest.raises(ValueError, match="unsafe archive member"):
        installer.install(entry, datasets_root / entry.install_path)

    assert list(datasets_root.iterdir()) == []


# --- API ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_returns_202_and_reports_progress_through_the_shared_row(
    api_app: FastAPI,
    client: httpx.AsyncClient,
    datasets_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = build_zip({"gsm8k/test.jsonl": "{}"})
    repinned = tuple(
        gsm8k_entry_for(payload) if entry.id == "gsm8k" else entry for entry in load_catalog()
    )
    monkeypatch.setattr("aisbench_web.api.datasets.load_catalog", lambda: repinned)
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
    response = await client.post("/api/datasets/synthetic/install")

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


def test_configs_that_differ_only_by_method_stay_distinct(aisbench_configs: Path) -> None:
    """gen and ppl are different evaluations; collapsing them loses which one will run."""
    gsm8k = next(entry for entry in load_catalog() if entry.id == "gsm8k")
    by_name = {config.name: config for config in gsm8k.configs}

    assert by_name["gsm8k_gen_0_shot_cot_str"].method == "gen"
    assert by_name["gsm8k_ppl_0_shot_str"].method == "ppl"
    # Every config is identified by its own file name, whatever it shares with the others.
    assert len({config.name for config in gsm8k.configs}) == len(gsm8k.configs)


def test_the_shortcut_config_marks_a_default_rather_than_duplicating_a_run(
    aisbench_configs: Path,
) -> None:
    """`<name>_gen.py` re-exports another config, so listing both offers one run twice."""
    gsm8k = next(entry for entry in load_catalog() if entry.id == "gsm8k")
    alias = next(config for config in gsm8k.configs if config.name == "gsm8k_gen")

    assert alias.alias_of == "gsm8k_gen_4_shot_cot_chat_prompt"
    offered = [config.name for config in gsm8k.configs_for("accuracy")]
    assert "gsm8k_gen" not in offered
    assert alias.alias_of in offered


def test_a_bare_shot_count_is_read_too(aisbench_configs: Path) -> None:
    """Real names use both `_5_shot_` and `_5shot_`; missing one made configs look identical."""
    from aisbench_web.datasets.scan import SHOTS

    assert SHOTS.search("math_prm800k_500_5shot_cot_gen").group(1) == "5"
    assert SHOTS.search("gsm8k_gen_4_shot_cot_chat_prompt").group(1) == "4"


# AISBench declares `attr` on every model config; hf_model.py documents it as "local or
# service". A config without one is not an endpoint, and the catalog logs what it skipped.
MODEL_CONFIG_SHAPES = {
    "declares_service": """
from ais_bench.benchmark.models import SomeAPI
models = [dict(attr="service", type=SomeAPI, abbr="a", host_ip="localhost", host_port=8080)]
""",
    "single_quoted_attr": """
from ais_bench.benchmark.models import SomeAPI
models = [dict(attr='service', type=SomeAPI, abbr='b', url='')]
""",
    "declares_local": """
from ais_bench.benchmark.models import SomeLocal
models = [dict(attr="local", type=SomeLocal, abbr="c", path="/models/Qwen")]
""",
    "declares_nothing": """
from ais_bench.benchmark.models import SomeLocal
models = [dict(type=SomeLocal, abbr="d", host_ip="localhost")]
""",
}


def test_only_a_config_declaring_service_drives_an_endpoint(tmp_path: Path) -> None:
    from aisbench_web.datasets.scan import scan_model_configs

    family = tmp_path / "ais_bench" / "benchmark" / "configs" / "models" / "some_family"
    family.mkdir(parents=True)
    for name, source in MODEL_CONFIG_SHAPES.items():
        (family / f"{name}.py").write_text(source, encoding="utf-8")

    scanned = {config.name: config for config in scan_model_configs(tmp_path / "ais_bench")}

    assert {name for name, config in scanned.items() if config.is_service} == {
        "declares_service",
        "single_quoted_attr",
    }
    # Found but not offered, which the catalog logs rather than passing over in silence.
    assert set(scanned) - {"declares_service", "single_quoted_attr"} == {
        "declares_local",
        "declares_nothing",
    }


def test_model_config_fields_come_from_the_file_not_from_a_fixed_list() -> None:
    """The CLI workflow is editing the chosen config file, so its own fields are the ones a
    job can change. The seven API configs AISBench ships do not agree on that list."""
    source = """
from ais_bench.benchmark.models import VLLMCustomAPIChat

models = [
    dict(
        attr="service",
        type=VLLMCustomAPIChat,
        abbr="vllm-api-stream-chat",
        path="",
        model="",
        stream=True,
        request_rate=0,
        retry=2,
        api_key="",
        host_ip="localhost",
        host_port=8080,
        url="",
        max_out_len=512,
        batch_size=1,
        trust_remote_code=False,
        generation_kwargs=dict(temperature=0.01, ignore_eos=False),
        pred_postprocessor=dict(type=extract_non_reasoning_content),
    )
]
"""

    from aisbench_web.datasets.scan import read_model_config_fields

    fields, generation = read_model_config_fields(source)

    # Declaration order, so the form reads like the file.
    assert [field.name for field in fields] == [
        "stream",
        "request_rate",
        "retry",
        "max_out_len",
        "batch_size",
        "trust_remote_code",
    ]
    assert [(field.name, field.default) for field in generation] == [
        ("temperature", 0.01),
        ("ignore_eos", False),
    ]
    assert {field.name: field.kind for field in fields}["stream"] == "boolean"
    assert {field.name: field.kind for field in fields}["max_out_len"] == "integer"


def test_fields_the_endpoint_supplies_are_not_asked_for_again() -> None:
    """The address and key come from the configured model endpoint, so a job never fills
    them in; `type` and `abbr` are not settings at all."""
    source = """
models = [dict(attr="service", type=Cls, abbr="a", path="", model="", model_name="",
               api_key="", host_ip="localhost", host_port=8080, url="", batch_size=1)]
"""

    from aisbench_web.datasets.scan import read_model_config_fields

    fields, _ = read_model_config_fields(source)

    assert [field.name for field in fields] == ["batch_size"]


def test_a_config_without_a_models_list_offers_nothing() -> None:
    from aisbench_web.datasets.scan import read_model_config_fields

    assert read_model_config_fields("x = 1\n") == ((), ())

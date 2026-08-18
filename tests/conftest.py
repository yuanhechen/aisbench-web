"""Shared pytest configuration for AISBench Web tests."""

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from aisbench_web.app import create_app
from aisbench_web.db import Database
from aisbench_web.settings import Settings

TEST_PASSWORD = "correct horse battery staple"

ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


DATASET_CONFIG = """\
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer

{symbol} = [
    dict(
        abbr='{dataset}',
        path='ais_bench/datasets/{data_path}',
        reader_cfg=dict(input_columns=['question'], output_column='answer'),
    )
]
"""

# A stand-in for the AISBench config tree the catalog reads. gsm8k and mmlu appear in the
# packaged download manifest; synthetic deliberately does not.
FAKE_DATASET_CONFIGS = {
    "gsm8k": ("gsm8k", [
        "gsm8k_gen_4_shot_cot_chat_prompt",
        "gsm8k_gen_0_shot_cot_str",
        "gsm8k_gen_0_shot_cot_str_perf",
        # Two configs that differ only by evaluation method, as several real datasets do.
        "gsm8k_ppl_0_shot_str",
    ]),
    "mmlu": ("mmlu", ["mmlu_gen_5_shot_chat_prompt"]),
    "synthetic": ("synthetic", ["synthetic_gen_string", "synthetic_gen_string_perf"]),
}
ALIAS_CONFIG = """\
from mmengine.config import read_base

with read_base():
    from .{target} import {symbol}
"""


@pytest.fixture
def aisbench_configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a small AISBench config tree and point the catalog at it."""
    package = tmp_path / "ais_bench"
    root = package / "benchmark" / "configs" / "datasets"
    for dataset, (data_path, configs) in FAKE_DATASET_CONFIGS.items():
        directory = root / dataset
        directory.mkdir(parents=True)
        for config in configs:
            (directory / f"{config}.py").write_text(
                DATASET_CONFIG.format(
                    symbol=f"{dataset}_datasets", dataset=dataset, data_path=data_path
                ),
                encoding="utf-8",
            )
        # AISBench ships a <name>_gen.py shortcut that re-exports one of the configs.
        (directory / f"{dataset}_gen.py").write_text(
            ALIAS_CONFIG.format(target=configs[0], symbol=f"{dataset}_datasets"),
            encoding="utf-8",
        )
    monkeypatch.setenv("AISBENCH_CONFIGS_PACKAGE", str(package))
    return package


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    created = Settings.create(tmp_path, tmp_path / "ais_bench", 1)
    created.ensure_layout()
    return created


@pytest.fixture
def api_app(settings: Settings, aisbench_configs: Path) -> FastAPI:
    return create_app(settings=settings, start_worker=False)


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings.db_path)


@pytest_asyncio.fixture
async def client_factory(api_app: FastAPI) -> AsyncIterator[ClientFactory]:
    """Yield a factory building one registered, signed-in client per username."""
    opened: list[httpx.AsyncClient] = []

    async def make_client(username: str) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app),
            base_url="http://testserver",
        )
        opened.append(client)
        registration = await client.post(
            "/api/auth/register",
            json={"username": username, "password": TEST_PASSWORD},
        )
        assert registration.status_code == 201, registration.text
        return client

    async with api_app.router.lifespan_context(api_app):
        try:
            yield make_client
        finally:
            for client in opened:
                await client.aclose()


@pytest_asyncio.fixture
async def anonymous_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        api_app.router.lifespan_context(api_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app),
            base_url="http://testserver",
        ) as client,
    ):
        yield client


@pytest_asyncio.fixture
async def client(client_factory: ClientFactory) -> httpx.AsyncClient:
    return await client_factory("alice")

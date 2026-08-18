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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    created = Settings.create(tmp_path, tmp_path / "ais_bench", 1)
    created.ensure_layout()
    return created


@pytest.fixture
def api_app(settings: Settings) -> FastAPI:
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

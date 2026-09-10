from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.router import ModelSpec, Registry

BACKEND_URL = "http://ollama.test:11434"

TEST_REGISTRY = Registry(
    [
        ModelSpec(
            name="qwen2.5:0.5b", backend_url=BACKEND_URL, capabilities=["chat", "completion"]
        ),
        ModelSpec(name="nomic-embed-text", backend_url=BACKEND_URL, capabilities=["embedding"]),
    ]
)


async def make_client(registry: Registry) -> AsyncIterator[AsyncClient]:
    """Client for an app built on `registry`, with the lifespan (app.state.http) entered."""
    app = create_app(registry)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async for c in make_client(TEST_REGISTRY):
        yield c

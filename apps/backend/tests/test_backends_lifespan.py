import asyncio

import pytest

from app.backends.ollama import OllamaBackend
from app.config import settings
from app.main import create_app
from tests.conftest import BACKEND_URL, TEST_REGISTRY


async def test_lifespan_builds_one_backend_per_url() -> None:
    app = create_app(TEST_REGISTRY)
    async with app.router.lifespan_context(app):
        assert set(app.state.backends) == {BACKEND_URL}
        assert isinstance(app.state.backends[BACKEND_URL], OllamaBackend)


async def test_shutdown_gives_up_on_a_wedged_usage_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write that never finishes must not hold shutdown past the flush timeout."""
    monkeypatch.setattr(settings, "usage_flush_timeout_s", 0.05)
    app = create_app(TEST_REGISTRY)
    async with app.router.lifespan_context(app):
        wedged = asyncio.create_task(asyncio.Event().wait())
        app.state.usage_tasks.add(wedged)

    assert wedged.cancelled()

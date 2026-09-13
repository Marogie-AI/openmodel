from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import create_app
from tests.conftest import TEST_REGISTRY


async def test_console_is_not_mounted_without_a_bundle() -> None:
    app = create_app(TEST_REGISTRY)

    assert not any(getattr(route, "path", None) == "/app" for route in app.routes)


async def test_console_serves_index_html_when_the_bundle_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html").write_text("<h1>console</h1>")
    monkeypatch.setattr(settings, "console_dir", tmp_path)
    app = create_app(TEST_REGISTRY)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/app/")

    assert response.status_code == 200
    assert response.text == "<h1>console</h1>"

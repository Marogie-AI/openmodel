from app.backends.ollama import OllamaBackend
from app.main import create_app
from tests.conftest import BACKEND_URL, TEST_REGISTRY


async def test_lifespan_builds_one_backend_per_url() -> None:
    app = create_app(TEST_REGISTRY)
    async with app.router.lifespan_context(app):
        assert set(app.state.backends) == {BACKEND_URL}
        assert isinstance(app.state.backends[BACKEND_URL], OllamaBackend)

from httpx import ASGITransport, AsyncClient

from app.errors import UnknownModel
from app.main import create_app
from tests.conftest import TEST_REGISTRY


async def test_api_error_renders_openai_envelope() -> None:
    app = create_app(TEST_REGISTRY)

    @app.get("/boom")
    async def boom() -> None:
        raise UnknownModel("ghost")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/boom")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "The model 'ghost' does not exist.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }

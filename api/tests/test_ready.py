import httpx
import respx
from httpx import AsyncClient

from app.main import create_app
from app.router import Registry
from tests.conftest import BACKEND_URL, TEST_REGISTRY, client_for, make_client

TAGS_URL = f"{BACKEND_URL}/api/tags"


@respx.mock
async def test_ready_when_backend_answers(client: AsyncClient) -> None:
    respx.get(TAGS_URL).respond(200, json={"models": []})

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@respx.mock
async def test_not_ready_reports_backend_error(client: AsyncClient) -> None:
    respx.get(TAGS_URL).mock(side_effect=httpx.ConnectError("refused"))

    response = await client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["backends"][BACKEND_URL].startswith("ConnectError")


@respx.mock
async def test_not_ready_when_backend_times_out(client: AsyncClient) -> None:
    respx.get(TAGS_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["backends"][BACKEND_URL].startswith("ReadTimeout")


@respx.mock
async def test_result_is_cached_within_ttl(client: AsyncClient) -> None:
    route = respx.get(TAGS_URL).respond(200, json={"models": []})

    await client.get("/ready")
    await client.get("/ready")

    assert route.call_count == 1


async def test_not_ready_when_registry_is_empty() -> None:
    async for client in make_client(Registry([])):
        response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "backends": {}}


@respx.mock
async def test_not_ready_when_redis_is_down() -> None:
    """Redis down means every limited route fails closed, so the pod reports not ready."""
    respx.get(TAGS_URL).respond(200, json={"models": []})

    async def boom() -> bool:
        raise ConnectionError("refused")

    app = create_app(TEST_REGISTRY)
    async with app.router.lifespan_context(app), client_for(app) as client:
        app.state.redis.ping = boom

        response = await client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["backends"][BACKEND_URL] == "ok"
    assert body["redis"] == "ConnectionError: refused"

"""Redis or Postgres down: the gateway fails closed rather than serving unlimited traffic."""

from collections.abc import Callable
from socket import gaierror
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.auth.principal import cache_key, require_principal
from app.main import create_app
from app.ratelimit import slot_for
from tests.conftest import TEST_PRINCIPAL, TEST_REGISTRY, NoopSlot, client_for

CHAT = {"model": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "hi"}]}


class BrokenRedis:
    """Every call raises, the way a redis client behaves while the server is unreachable."""

    def __getattr__(self, name: str) -> Any:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RedisConnectionError("Error connecting to redis:6379")

        return boom


class DeadSession:
    """A session whose queries fail. Commit is a no-op: a session that never connected has
    nothing to commit, which is exactly why a cache hit survives the outage."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self) -> "DeadSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def _dead(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error

    get = scalar = scalars = execute = _dead


def dead_sessionmaker(error: Exception) -> Callable[[], DeadSession]:
    return lambda: DeadSession(error)


# The two ways Postgres goes away: the driver reports it (SQLAlchemy wraps), or the socket never
# opens (asyncpg raises the OSError raw).
DB_ERRORS = [
    OperationalError("SELECT 1", {}, Exception("server closed the connection")),
    gaierror(-2, "Name or service not known"),
]


async def test_redis_down_fails_llm_requests_closed() -> None:
    app = create_app(TEST_REGISTRY)
    app.dependency_overrides[require_principal] = lambda: TEST_PRINCIPAL
    app.dependency_overrides[slot_for] = NoopSlot
    async with app.router.lifespan_context(app), client_for(app) as client:
        app.state.redis = BrokenRedis()
        response = await client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "message": "Rate limiting is unavailable; the request was not served.",
            "type": "server_error",
            "code": "rate_limiter_unavailable",
        }
    }


@pytest.mark.db
async def test_cached_key_survives_a_postgres_outage(
    db_app: FastAPI, auth_client: AsyncClient, principal_key: str
) -> None:
    # First call fills the auth cache from Postgres; then Postgres goes away.
    assert (await auth_client.get("/v1/models")).status_code == 200
    db_app.state.sessionmaker = dead_sessionmaker(DB_ERRORS[0])

    assert (await auth_client.get("/v1/models")).status_code == 200


@pytest.mark.db
@pytest.mark.parametrize("error", DB_ERRORS)
async def test_uncached_key_fails_closed_when_postgres_is_down(
    db_app: FastAPI, auth_client: AsyncClient, principal_key: str, error: Exception
) -> None:
    await db_app.state.redis.delete(cache_key(principal_key.split("_")[2]))
    db_app.state.sessionmaker = dead_sessionmaker(error)

    response = await auth_client.get("/v1/models")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


async def test_an_unhandled_exception_still_renders_the_envelope() -> None:
    app = create_app(TEST_REGISTRY)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"message": "Internal server error", "type": "server_error", "code": None}
    }

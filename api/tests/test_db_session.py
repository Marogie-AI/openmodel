from collections.abc import AsyncIterator
from typing import Annotated

import pytest
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.main import create_app
from tests.conftest import TEST_REGISTRY

pytestmark = pytest.mark.db


def app_with(route: object) -> FastAPI:
    app = create_app(TEST_REGISTRY)
    app.add_api_route("/probe", route)  # type: ignore[arg-type]
    return app


async def client_for(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def test_get_session_yields_usable_session() -> None:
    async def probe(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, int]:
        return {"one": (await session.execute(text("SELECT 1"))).scalar_one()}

    async for client in client_for(app_with(probe)):
        response = await client.get("/probe")
        assert response.status_code == 200
        assert response.json() == {"one": 1}


async def test_get_session_closes_session_when_handler_raises() -> None:
    seen: list[AsyncSession] = []

    async def probe(session: Annotated[AsyncSession, Depends(get_session)]) -> None:
        seen.append(session)
        await session.execute(text("SELECT 1"))
        raise RuntimeError("boom")

    async for client in client_for(app_with(probe)):
        with pytest.raises(RuntimeError, match="boom"):
            await client.get("/probe")

    # Tables land in Task 2; until then "rolled back" shows up as a closed, idle session.
    assert len(seen) == 1
    assert not seen[0].in_transaction()


async def test_session_fixture_is_usable(session: AsyncSession) -> None:
    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_redis_client_fixture_starts_empty(redis_client: aioredis.Redis) -> None:
    assert await redis_client.dbsize() == 0
    await redis_client.set("k", "v")
    assert await redis_client.get("k") == "v"

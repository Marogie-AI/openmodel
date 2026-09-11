import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import settings
from app.db.seed import seed_plans
from app.db.session import make_engine, make_sessionmaker
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


async def db_available() -> bool:
    """True when the compose postgres accepts a connection from the app role."""
    engine = make_engine(settings.database_url)
    try:
        conn = await asyncio.wait_for(engine.connect(), timeout=1)
        await conn.close()
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


async def redis_available() -> bool:
    """True when the compose redis answers PING."""
    client = aioredis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        await asyncio.wait_for(client.ping(), timeout=1)
    except Exception:
        return False
    finally:
        await client.aclose()
    return True


_services_up: bool | None = None
_migrated = False

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def alembic_config() -> Config:
    """Alembic config for the committed alembic.ini, usable outside the CLI."""
    config = Config(str(ALEMBIC_INI))
    config.cmd_opts = None
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(autouse=True)
async def skip_without_services(request: pytest.FixtureRequest) -> None:
    """Skip tests marked `db` when postgres/redis are not reachable (probed once)."""
    if request.node.get_closest_marker("db") is None:
        return
    global _services_up
    if _services_up is None:
        _services_up = await db_available() and await redis_available()
    if not _services_up:
        pytest.skip("postgres/redis not reachable")
    global _migrated
    if not _migrated:
        await asyncio.to_thread(command.upgrade, alembic_config(), "head")
        _migrated = True


TABLES = "plan_model, request, api_key, app_user, organization, plan"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Owner session on a database holding nothing but the seeded plans."""
    engine = make_engine(settings.database_owner_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await s.commit()
        await seed_plans(s)
        yield s
    await engine.dispose()


@pytest.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    """Redis client on an empty database."""
    client: aioredis.Redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()

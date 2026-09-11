import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import redis.asyncio as aioredis
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.auth.principal import Principal, require_principal
from app.config import settings
from app.db.seed import seed_plans
from app.db.session import make_engine, make_sessionmaker
from app.main import create_app
from app.ratelimit import ConcurrencySlot, enforce, slot_for
from app.router import ModelSpec, Registry
from app.usage import UsageRecord

# Filled by the stub sink `make_client` installs, cleared per client.
USAGE_RECORDS: list[UsageRecord] = []

BACKEND_URL = "http://ollama.test:11434"

TEST_REGISTRY = Registry(
    [
        ModelSpec(
            name="qwen2.5:0.5b", backend_url=BACKEND_URL, capabilities=["chat", "completion"]
        ),
        ModelSpec(name="nomic-embed-text", backend_url=BACKEND_URL, capabilities=["embedding"]),
    ]
)


TEST_PRINCIPAL = Principal(
    api_key_id=UUID("00000000-0000-0000-0000-0000000000a1"),
    user_id=UUID("00000000-0000-0000-0000-0000000000b1"),
    organization_id=UUID("00000000-0000-0000-0000-0000000000c1"),
    plan_code="free",
    requests_per_minute=60,
    max_concurrency=2,
    models=frozenset({"qwen2.5:0.5b", "nomic-embed-text"}),
)


class NoopSlot(ConcurrencySlot):
    """A concurrency slot that touches no Redis, for the route tests that run without one."""

    def __init__(self) -> None:
        pass

    async def acquire(self) -> None:
        return None

    async def release(self) -> None:
        return None


async def make_client(registry: Registry) -> AsyncIterator[AsyncClient]:
    """Client for an app built on `registry`, with the lifespan (app.state.http) entered.

    Auth is stubbed out to TEST_PRINCIPAL, so these route tests need no database.
    """
    app = create_app(registry)
    app.dependency_overrides[require_principal] = lambda: TEST_PRINCIPAL
    # `enforce` and the slot too: these tests exercise routes, not Redis. Limits are tested
    # through `auth_client` against the real Redis.
    app.dependency_overrides[enforce] = lambda: TEST_PRINCIPAL
    app.dependency_overrides[slot_for] = NoopSlot
    USAGE_RECORDS.clear()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        # The lifespan installed the real background writer; these tests have no database.
        app.state.usage_sink = USAGE_RECORDS.append
        yield client


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async for c in make_client(TEST_REGISTRY):
        yield c


@pytest.fixture
def usage_records(client: AsyncClient) -> list[UsageRecord]:
    """Usage records the routes handed to the sink, in order."""
    return USAGE_RECORDS


ADMIN_HEADERS = {"X-Admin-Token": settings.admin_token}


@pytest.fixture
async def db_app(session: AsyncSession) -> AsyncIterator[FastAPI]:
    """App on the real postgres/redis and the real auth, with lifespan entered."""
    app = create_app(TEST_REGISTRY)
    async with app.router.lifespan_context(app):
        yield app


def client_for(app: FastAPI, headers: dict[str, str] | None = None) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


@pytest.fixture
async def db_client(db_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Unauthenticated client on the real postgres/redis, tables truncated, plans seeded."""
    async with client_for(db_app) as c:
        yield c


@pytest.fixture
async def principal_key(db_client: AsyncClient) -> str:
    """A raw API key for a fresh user of a fresh organization on the `free` plan."""
    org = await db_client.post(
        "/admin/orgs", json={"name": "acme", "plan_code": "free"}, headers=ADMIN_HEADERS
    )
    user = await db_client.post(
        "/admin/users",
        json={"organization_id": org.json()["id"], "email": "principal@example.com"},
        headers=ADMIN_HEADERS,
    )
    key = await db_client.post(f"/admin/users/{user.json()['id']}/keys", headers=ADMIN_HEADERS)
    raw: str = key.json()["key"]
    return raw


@pytest.fixture
async def auth_client(db_app: FastAPI, principal_key: str) -> AsyncIterator[AsyncClient]:
    """A second client on the same app, carrying the bearer header of `principal_key`."""
    async with client_for(db_app, {"Authorization": f"Bearer {principal_key}"}) as c:
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


@pytest.fixture
def record_to_thread(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record the callables handed to asyncio.to_thread, still running each one."""
    calls: list[object] = []
    original = asyncio.to_thread

    async def recording(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording)
    return calls

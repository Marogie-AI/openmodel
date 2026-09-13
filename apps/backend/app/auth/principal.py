"""Bearer API-key auth: resolve a raw key to a Principal, cached in Redis."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Header, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.keys import parse_key, verify_secret
from app.config import settings
from app.db.models import ApiKey, PlanModel
from app.db.session import translate_db_errors
from app.errors import Unauthorized

# One message for every failure mode: an unknown key_id must not be distinguishable from a bad
# secret.
INVALID_KEY = "Missing or invalid API key"


@dataclass(frozen=True)
class Principal:
    api_key_id: UUID
    user_id: UUID
    organization_id: UUID
    plan_code: str
    requests_per_minute: int
    max_concurrency: int
    models: frozenset[str]


def cache_key(key_id: str) -> str:
    return f"auth:{key_id}"


def plan_keys(plan_code: str) -> str:
    """Set of cached key_ids on a plan, so a plan edit can drop exactly those entries."""
    return f"plan_keys:{plan_code}"


def _fast_hash(secret: str) -> str:
    """Cheap peppered digest of the secret. Only ever stored in Redis, under the cache TTL."""
    return hashlib.sha256((secret + settings.key_pepper).encode()).hexdigest()


async def drop_cached(redis: Redis, key_id: str) -> None:
    await redis.delete(cache_key(key_id))


async def drop_plan_cached(redis: Redis, plan_code: str) -> None:
    """Drop every cached principal on a plan; call after the plan's limits or models change."""
    # The app client is decode_responses=True, so members come back as str.
    key_ids = cast(set[str], await redis.smembers(plan_keys(plan_code)))
    if key_ids:
        await redis.delete(*(cache_key(key_id) for key_id in key_ids))
    await redis.delete(plan_keys(plan_code))


def _principal(cached: dict[str, Any]) -> Principal:
    return Principal(
        api_key_id=UUID(cached["api_key_id"]),
        user_id=UUID(cached["user_id"]),
        organization_id=UUID(cached["organization_id"]),
        plan_code=cached["plan_code"],
        requests_per_minute=cached["requests_per_minute"],
        max_concurrency=cached["max_concurrency"],
        models=frozenset(cached["models"]),
    )


async def _load(key_id: str, secret: str, session: AsyncSession) -> dict[str, Any]:
    key = await session.scalar(select(ApiKey).where(ApiKey.key_id == key_id))
    if key is None or key.revoked_at is not None:
        raise Unauthorized(INVALID_KEY)
    # argon2 costs ~20ms of CPU, so keep it off the event loop.
    if not await asyncio.to_thread(verify_secret, key.secret_hash, secret, settings.key_pepper):
        raise Unauthorized(INVALID_KEY)

    org = key.user.organization
    models = await session.scalars(
        select(PlanModel.model_name).where(PlanModel.plan_code == org.plan_code)
    )
    return {
        "api_key_id": str(key.id),
        "user_id": str(key.user_id),
        "organization_id": str(org.id),
        "plan_code": org.plan_code,
        "requests_per_minute": org.plan.requests_per_minute,
        "max_concurrency": org.plan.max_concurrency,
        "models": sorted(models.all()),
        "fast_hash": _fast_hash(secret),
    }


async def resolve_principal(
    raw_key: str, sessionmaker: async_sessionmaker[AsyncSession], redis: Redis
) -> Principal:
    parsed = parse_key(raw_key)
    if parsed is None:
        raise Unauthorized(INVALID_KEY)
    key_id, secret = parsed

    raw_cached = await redis.get(cache_key(key_id))
    if raw_cached is not None:
        cached: dict[str, Any] = json.loads(raw_cached)
        # No DB fallback on a mismatch: the cache entry is authoritative for its TTL.
        if not hmac.compare_digest(cached["fast_hash"], _fast_hash(secret)):
            raise Unauthorized(INVALID_KEY)
        return _principal(cached)

    # Its own short session, closed before the caller's real work starts: on the request-scoped
    # session the autobegun read transaction would sit idle for the whole LLM call, and a handful
    # of cache misses would hold the pool open until Postgres killed the connections.
    async with translate_db_errors(), sessionmaker() as session:
        fresh = await _load(key_id, secret, session)
    # Index first: a cache entry the plan set does not know about would survive a plan edit.
    plan_set = plan_keys(fresh["plan_code"])
    await redis.sadd(plan_set, key_id)
    await redis.expire(plan_set, settings.auth_cache_ttl_s)
    await redis.set(cache_key(key_id), json.dumps(fresh), ex=settings.auth_cache_ttl_s)
    return _principal(fresh)


async def require_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    scheme, _, raw_key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not raw_key:
        raise Unauthorized(INVALID_KEY)
    return await resolve_principal(
        raw_key.strip(), request.app.state.sessionmaker, request.app.state.redis
    )

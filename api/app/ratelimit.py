"""Per-organization rate limiting: a Redis token bucket and a concurrency cap."""

import math
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.auth.principal import Principal, require_principal
from app.errors import RateLimited

TOKEN_BUCKET_LUA = """
local key = KEYS[1]; local rate = tonumber(ARGV[1]); local burst = tonumber(ARGV[2]); local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1]); local ts = tonumber(data[2])
if tokens == nil then tokens = burst; ts = now end
tokens = math.min(burst, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(burst / rate * 1000) + 1000)
local retry_ms = 0
if allowed == 0 then retry_ms = math.ceil((1 - tokens) / rate * 1000) end
return {allowed, math.floor(tokens), retry_ms}
"""  # noqa: E501

# DECR can go negative if a slot is released twice (a crash between INCR and the guard); clamp it.
RELEASE_SLOT_LUA = """
local v = redis.call('DECR', KEYS[1])
if v < 0 then redis.call('SET', KEYS[1], 0) end
"""

# A held slot outlives its request only if the process dies mid-stream; expire it rather than leak.
SLOT_TTL_S = 300


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    limit: int
    retry_after_s: int


async def take_token(redis: Redis, org_id: UUID, rpm: int, now: float | None = None) -> Decision:
    """Spend one token from the org's bucket: `rpm` per minute, bursting up to `rpm`."""
    script = redis.register_script(TOKEN_BUCKET_LUA)
    result: list[Any] = await script(
        keys=[f"rl:{org_id}"], args=[rpm / 60, rpm, time.time() if now is None else now]
    )
    allowed, remaining, retry_ms = (int(value) for value in result)
    return Decision(
        allowed=bool(allowed),
        remaining=remaining,
        limit=rpm,
        retry_after_s=math.ceil(retry_ms / 1000),
    )


async def enforce(
    request: Request, principal: Annotated[Principal, Depends(require_principal)]
) -> Principal:
    """Authenticate, then spend a token. Stashes the decision for the response headers."""
    decision = await take_token(
        request.app.state.redis, principal.organization_id, principal.requests_per_minute
    )
    request.state.rate = decision
    if not decision.allowed:
        raise RateLimited(
            f"Rate limit reached for organization: {decision.limit} requests per minute.",
            retry_after_s=decision.retry_after_s,
            limit=decision.limit,
            remaining=0,
        )
    return principal


class ConcurrencySlot:
    """Holds one of an organization's in-flight request slots until it is released."""

    def __init__(self, redis: Redis, org_id: UUID, limit: int) -> None:
        self.redis = redis
        self.key = f"cc:{org_id}"
        self.limit = limit

    async def acquire(self) -> None:
        """Take a slot, or raise RateLimited when the organization is already at its cap."""
        held = int(await self.redis.incr(self.key))
        try:
            if held == 1:
                # Only on create: refreshing the TTL on every enter would keep a leaked
                # increment alive forever on a busy organization.
                await self.redis.expire(self.key, SLOT_TTL_S)
        except Exception:
            await self.release()
            raise
        if held > self.limit:
            await self.release()
            raise RateLimited(
                "Too many concurrent requests", retry_after_s=1, limit=self.limit, remaining=0
            )

    async def release(self) -> None:
        await self.redis.register_script(RELEASE_SLOT_LUA)(keys=[self.key])

    async def __aenter__(self) -> "ConcurrencySlot":
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()


def slot_for(
    request: Request, principal: Annotated[Principal, Depends(enforce)]
) -> ConcurrencySlot:
    """Dependency so tests can swap the slot out; the routes never build one themselves."""
    return ConcurrencySlot(
        request.app.state.redis, principal.organization_id, principal.max_concurrency
    )

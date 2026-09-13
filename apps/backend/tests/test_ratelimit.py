import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import redis.asyncio as aioredis
import respx
from httpx import AsyncClient

from app.auth.principal import cache_key, plan_keys
from app.errors import RateLimited
from app.ratelimit import ConcurrencySlot, take_token
from tests.conftest import ADMIN_HEADERS, BACKEND_URL
from tests.test_auth import key_id_of

pytestmark = pytest.mark.db

CHAT_URL = f"{BACKEND_URL}/api/chat"
FREE_MODELS = ["nomic-embed-text", "qwen2.5:0.5b"]

BODY: dict[str, Any] = {
    "model": "qwen2.5:0.5b",
    "messages": [{"role": "user", "content": "yo"}],
}


def ollama_reply() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": "hi"},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 1,
            "eval_count": 1,
        },
    )


async def set_free_plan(
    db_client: AsyncClient,
    *,
    rpm: int = 60,
    concurrency: int = 2,
    models: list[str] | None = None,
) -> None:
    response = await db_client.put(
        "/admin/plans/free",
        json={
            "requests_per_minute": rpm,
            "max_concurrency": concurrency,
            "models": FREE_MODELS if models is None else models,
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200


async def concurrency_count(redis: aioredis.Redis) -> int:
    """The single live `cc:` counter, or 0 when none was ever taken."""
    keys = await redis.keys("cc:*")
    if not keys:
        return 0
    value = await redis.get(keys[0])
    assert isinstance(value, str)
    return int(value)


# --- token bucket ------------------------------------------------------------


async def test_bucket_allows_a_full_minutes_burst_then_denies(
    redis_client: aioredis.Redis,
) -> None:
    org = uuid4()

    for _ in range(60):
        assert (await take_token(redis_client, org, 60, now=1000.0)).allowed

    denied = await take_token(redis_client, org, 60, now=1000.0)

    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.limit == 60
    assert denied.retry_after_s == 1


async def test_bucket_remaining_counts_down(redis_client: aioredis.Redis) -> None:
    org = uuid4()

    first = await take_token(redis_client, org, 60, now=1000.0)
    second = await take_token(redis_client, org, 60, now=1000.0)

    assert (first.remaining, second.remaining) == (59, 58)


async def test_bucket_refills_over_time(redis_client: aioredis.Redis) -> None:
    org = uuid4()
    for _ in range(60):
        await take_token(redis_client, org, 60, now=1000.0)
    assert (await take_token(redis_client, org, 60, now=1000.0)).allowed is False

    # One token per second at 60 rpm, so a second later exactly one more request gets through.
    assert (await take_token(redis_client, org, 60, now=1001.0)).allowed is True
    assert (await take_token(redis_client, org, 60, now=1001.0)).allowed is False


async def test_buckets_are_per_organization(redis_client: aioredis.Redis) -> None:
    a, b = uuid4(), uuid4()
    assert (await take_token(redis_client, a, 1, now=1000.0)).allowed is True

    assert (await take_token(redis_client, a, 1, now=1000.0)).allowed is False
    assert (await take_token(redis_client, b, 1, now=1000.0)).allowed is True


# --- concurrency slots -------------------------------------------------------


def slot(redis: aioredis.Redis, org: UUID, limit: int = 2) -> ConcurrencySlot:
    return ConcurrencySlot(redis, org, limit)


async def test_slot_admits_up_to_the_limit_and_refuses_the_next(
    redis_client: aioredis.Redis,
) -> None:
    org = uuid4()

    async with slot(redis_client, org), slot(redis_client, org):
        assert await concurrency_count(redis_client) == 2
        with pytest.raises(RateLimited) as caught:
            async with slot(redis_client, org):
                pass

    assert caught.value.status_code == 429
    assert caught.value.message == "Too many concurrent requests"
    assert (caught.value.limit, caught.value.remaining) == (2, 0)
    # The refused attempt gave its increment back, and both holders released on exit.
    assert await concurrency_count(redis_client) == 0


async def test_slot_is_released_when_the_body_raises(redis_client: aioredis.Redis) -> None:
    org = uuid4()

    with pytest.raises(RuntimeError):
        async with slot(redis_client, org):
            raise RuntimeError("boom")

    assert await concurrency_count(redis_client) == 0


async def test_slot_never_counts_below_zero(redis_client: aioredis.Redis) -> None:
    org = uuid4()

    async with slot(redis_client, org):
        pass
    await ConcurrencySlot(redis_client, org, 2).release()

    assert await concurrency_count(redis_client) == 0


async def test_slot_ttl_is_set_on_create_and_not_refreshed(redis_client: aioredis.Redis) -> None:
    org = uuid4()
    first = slot(redis_client, org)
    await first.acquire()
    created_ttl = await redis_client.pttl(first.key)
    assert 0 < created_ttl <= 300_000

    await asyncio.sleep(0.05)
    second = slot(redis_client, org)
    await second.acquire()

    # A leaked increment must still expire, so a second enter may not push the TTL out.
    assert await redis_client.pttl(first.key) < created_ttl
    await second.release()
    await first.release()


# --- enforcement on the LLM routes -------------------------------------------


@respx.mock
async def test_successful_call_carries_ratelimit_headers(
    redis_client: aioredis.Redis, db_client: AsyncClient, auth_client: AsyncClient
) -> None:
    await set_free_plan(db_client, rpm=60)
    respx.post(CHAT_URL).mock(return_value=ollama_reply())

    response = await auth_client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit"] == "60"
    assert response.headers["x-ratelimit-remaining"] == "59"


@respx.mock
async def test_requests_beyond_the_plan_rate_get_a_429_envelope(
    redis_client: aioredis.Redis, db_client: AsyncClient, auth_client: AsyncClient
) -> None:
    await set_free_plan(db_client, rpm=2)
    respx.post(CHAT_URL).mock(return_value=ollama_reply())

    allowed = [await auth_client.post("/v1/chat/completions", json=BODY) for _ in range(2)]
    denied = await auth_client.post("/v1/chat/completions", json=BODY)

    assert [response.status_code for response in allowed] == [200, 200]
    assert denied.status_code == 429
    assert denied.json() == {
        "error": {
            "message": "Rate limit reached for organization: 2 requests per minute.",
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }
    }
    assert denied.headers["retry-after"] == "30"
    assert denied.headers["x-ratelimit-limit"] == "2"
    assert denied.headers["x-ratelimit-remaining"] == "0"


async def test_model_outside_the_plan_is_a_403(
    redis_client: aioredis.Redis, db_client: AsyncClient, auth_client: AsyncClient
) -> None:
    await set_free_plan(db_client, models=["nomic-embed-text"])

    response = await auth_client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "message": "Your plan does not include the model 'qwen2.5:0.5b'.",
            "type": "invalid_request_error",
            "code": "model_not_allowed",
        }
    }


async def test_unknown_model_is_still_a_404(
    redis_client: aioredis.Redis, db_client: AsyncClient, auth_client: AsyncClient
) -> None:
    await set_free_plan(db_client, models=[])

    response = await auth_client.post("/v1/chat/completions", json={**BODY, "model": "ghost"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


@respx.mock
async def test_the_plan_concurrency_cap_refuses_the_extra_request(
    redis_client: aioredis.Redis, db_client: AsyncClient, auth_client: AsyncClient
) -> None:
    await set_free_plan(db_client, concurrency=1)
    respx.post(CHAT_URL).mock(return_value=ollama_reply())
    # Warm the auth cache so the held slot below is the only thing in the org's way.
    await auth_client.post("/v1/chat/completions", json=BODY)
    org_key = (await redis_client.keys("cc:*"))[0]
    assert isinstance(org_key, str)
    org = UUID(org_key.removeprefix("cc:"))

    async with ConcurrencySlot(redis_client, org, 1):
        response = await auth_client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 429
    assert response.json()["error"]["message"] == "Too many concurrent requests"
    assert response.headers["x-ratelimit-limit"] == "1"
    assert response.headers["retry-after"] == "1"


@respx.mock
async def test_a_stream_over_the_cap_is_a_429_before_any_chunk(
    redis_client: aioredis.Redis, db_client: AsyncClient, auth_client: AsyncClient
) -> None:
    await set_free_plan(db_client, concurrency=1)
    respx.post(CHAT_URL).mock(return_value=ollama_reply())
    await auth_client.post("/v1/chat/completions", json=BODY)  # warms the auth cache
    org_key = (await redis_client.keys("cc:*"))[0]
    assert isinstance(org_key, str)
    org = UUID(org_key.removeprefix("cc:"))

    async with ConcurrencySlot(redis_client, org, 1):
        response = await auth_client.post("/v1/chat/completions", json={**BODY, "stream": True})

    assert response.status_code == 429
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"] == {
        "message": "Too many concurrent requests",
        "type": "rate_limit_error",
        "code": "rate_limit_exceeded",
    }


@respx.mock
async def test_a_stream_holds_its_slot_until_the_last_chunk(
    redis_client: aioredis.Redis, db_client: AsyncClient, auth_client: AsyncClient
) -> None:
    await set_free_plan(db_client)
    mid_stream: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        yield json.dumps({"message": {"content": "hi"}, "done": False}).encode() + b"\n"
        mid_stream.append(await concurrency_count(redis_client))
        yield json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n"

    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=body()))

    response = await auth_client.post("/v1/chat/completions", json={**BODY, "stream": True})

    assert response.status_code == 200
    assert response.text.endswith("data: [DONE]\n\n")
    assert mid_stream == [1]
    assert await concurrency_count(redis_client) == 0


# --- cache invalidation on a plan change -------------------------------------


@respx.mock
async def test_editing_a_plan_drops_its_cached_principals(
    redis_client: aioredis.Redis,
    db_client: AsyncClient,
    auth_client: AsyncClient,
    principal_key: str,
) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_reply())
    assert (await auth_client.post("/v1/chat/completions", json=BODY)).status_code == 200
    key_id = key_id_of(principal_key)
    assert await redis_client.exists(cache_key(key_id)) == 1

    await set_free_plan(db_client, models=["nomic-embed-text"])

    assert await redis_client.exists(cache_key(key_id)) == 0
    assert await redis_client.exists(plan_keys("free")) == 0
    # The next call resolves the principal afresh, so the narrowed model list takes effect at once.
    assert (await auth_client.post("/v1/chat/completions", json=BODY)).status_code == 403

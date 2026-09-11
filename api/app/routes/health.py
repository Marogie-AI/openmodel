import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.router import Registry

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _probe(http: httpx.AsyncClient, registry: Registry) -> dict[str, str]:
    """Map each backend url to "ok" or the error that made it unreachable."""
    results = {}
    for url in sorted(registry.backend_urls()):
        try:
            response = await http.get(f"{url}/api/tags", timeout=settings.backend_connect_timeout_s)
            results[url] = "ok" if response.status_code == 200 else f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            results[url] = f"{type(exc).__name__}: {exc}"
    return results


async def _ping_redis(redis: Any) -> str | None:
    """None when Redis answers in time, else the error that stopped it."""
    try:
        await asyncio.wait_for(redis.ping(), 1.0)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    cached: tuple[float, dict[str, str], str | None] | None = getattr(state, "ready_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < settings.ready_cache_ttl_s:
        _, backends, redis_error = cached
    else:
        backends = await _probe(state.http, state.registry)
        # Without Redis every limited route fails closed, so the pod is not ready either.
        redis_error = await _ping_redis(state.redis)
        state.ready_cache = (now, backends, redis_error)

    is_ready = (
        bool(backends)
        and all(status == "ok" for status in backends.values())
        and redis_error is None
    )
    body: dict[str, Any] = {"status": "ready"} if is_ready else {"status": "not_ready"}
    if not is_ready:
        body["backends"] = backends
        if redis_error is not None:
            body["redis"] = redis_error
    return JSONResponse(status_code=200 if is_ready else 503, content=body)

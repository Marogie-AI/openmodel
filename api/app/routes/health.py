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
            response = await http.get(f"{url}/api/tags")
            results[url] = "ok" if response.status_code == 200 else f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            results[url] = f"{type(exc).__name__}: {exc}"
    return results


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    cached: tuple[float, dict[str, str]] | None = getattr(state, "ready_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < settings.ready_cache_ttl_s:
        backends = cached[1]
    else:
        backends = await _probe(state.http, state.registry)
        state.ready_cache = (now, backends)

    is_ready = bool(backends) and all(status == "ok" for status in backends.values())
    body: dict[str, Any] = (
        {"status": "ready"} if is_ready else {"status": "not_ready", "backends": backends}
    )
    return JSONResponse(status_code=200 if is_ready else 503, content=body)

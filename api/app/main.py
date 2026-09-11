import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import httpx
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth.principal import require_principal
from app.backends.ollama import OllamaBackend
from app.config import settings
from app.db.session import make_engine, make_sessionmaker
from app.errors import ApiError, InvalidRequest, RateLimited, envelope
from app.logging import RequestIdMiddleware, configure_logging
from app.metrics import MetricsMiddleware
from app.ratelimit import enforce
from app.router import Registry
from app.routes import admin, chat, completions, embeddings, health, keys, metrics, models, usage
from app.usage import schedule_write


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    timeout = httpx.Timeout(
        connect=settings.backend_connect_timeout_s,
        read=settings.backend_read_timeout_s,
        write=10,
        pool=10,
    )
    engine = make_engine(settings.database_url)
    usage_tasks: set[asyncio.Task[None]] = set()
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    async with httpx.AsyncClient(timeout=timeout) as http:
        app.state.http = http
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.redis = redis_client
        app.state.usage_tasks = usage_tasks
        app.state.usage_sink = partial(schedule_write, app.state.sessionmaker, usage_tasks)
        registry: Registry = app.state.registry
        app.state.backends = {
            url: OllamaBackend(client=http, base_url=url, retries=settings.backend_retries)
            for url in registry.backend_urls()
        }
        try:
            yield
        finally:
            # Metering is fire-and-forget, so shutdown is where pending writes get to land.
            await asyncio.gather(*usage_tasks, return_exceptions=True)
            await redis_client.aclose()
            await engine.dispose()


def _error_headers(exc: ApiError) -> dict[str, str] | None:
    if exc.status_code == 401:
        return {"WWW-Authenticate": "Bearer"}
    if isinstance(exc, RateLimited):
        return {
            "Retry-After": str(exc.retry_after_s),
            "X-RateLimit-Limit": str(exc.limit),
            "X-RateLimit-Remaining": str(exc.remaining),
        }
    return None


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code, content=envelope(exc), headers=_error_headers(exc)
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    summary = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
    return await api_error_handler(request, InvalidRequest(summary))


def create_app(registry: Registry | None = None) -> FastAPI:
    configure_logging(settings.log_level)
    app = FastAPI(title="OpenModel API", lifespan=lifespan)
    app.state.registry = registry or Registry.from_yaml(settings.models_file)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(admin.router)
    app.include_router(health.router)
    authed = [Depends(require_principal)]
    limited = [Depends(enforce)]
    app.include_router(models.router, dependencies=authed)
    app.include_router(chat.router, dependencies=limited)
    app.include_router(completions.router, dependencies=limited)
    app.include_router(embeddings.router, dependencies=limited)
    app.include_router(keys.router, dependencies=authed)
    app.include_router(usage.router, dependencies=authed)
    app.include_router(metrics.router)
    return app


app = create_app()

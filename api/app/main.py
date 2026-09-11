from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.auth.principal import require_principal
from app.backends.ollama import OllamaBackend
from app.config import settings
from app.db.session import make_engine, make_sessionmaker
from app.errors import ApiError, InvalidRequest, envelope
from app.logging import RequestIdMiddleware, configure_logging
from app.metrics import MetricsMiddleware
from app.router import Registry
from app.routes import admin, chat, completions, embeddings, health, keys, metrics, models


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    timeout = httpx.Timeout(
        connect=settings.backend_connect_timeout_s,
        read=settings.backend_read_timeout_s,
        write=10,
        pool=10,
    )
    engine = make_engine(settings.database_url)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    async with httpx.AsyncClient(timeout=timeout) as http:
        app.state.http = http
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.redis = redis_client
        registry: Registry = app.state.registry
        app.state.backends = {
            url: OllamaBackend(client=http, base_url=url, retries=settings.backend_retries)
            for url in registry.backend_urls()
        }
        try:
            yield
        finally:
            await redis_client.aclose()
            await engine.dispose()


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope(exc),
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
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
    app.include_router(models.router, dependencies=authed)
    app.include_router(chat.router, dependencies=authed)
    app.include_router(completions.router, dependencies=authed)
    app.include_router(embeddings.router, dependencies=authed)
    app.include_router(keys.router, dependencies=authed)
    app.include_router(metrics.router)
    return app


app = create_app()

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.backends.ollama import OllamaBackend
from app.config import settings
from app.errors import ApiError, envelope
from app.logging import RequestIdMiddleware, configure_logging
from app.router import Registry
from app.routes import chat, health, models


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    timeout = httpx.Timeout(
        connect=settings.backend_connect_timeout_s,
        read=settings.backend_read_timeout_s,
        write=10,
        pool=10,
    )
    async with httpx.AsyncClient(timeout=timeout) as http:
        app.state.http = http
        registry: Registry = app.state.registry
        app.state.backends = {
            url: OllamaBackend(client=http, base_url=url, retries=settings.backend_retries)
            for url in registry.backend_urls()
        }
        yield


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope(exc),
    )


def create_app(registry: Registry | None = None) -> FastAPI:
    configure_logging(settings.log_level)
    app = FastAPI(title="OpenModel API", lifespan=lifespan)
    app.state.registry = registry or Registry.from_yaml(settings.models_file)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    return app


app = create_app()

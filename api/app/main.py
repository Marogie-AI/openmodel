from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.errors import ApiError
from app.logging import RequestIdMiddleware, configure_logging
from app.router import Registry
from app.routes import health, models


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": exc.type, "code": exc.code}},
    )


def create_app(registry: Registry | None = None) -> FastAPI:
    configure_logging(settings.log_level)
    app = FastAPI(title="OpenModel API")
    app.state.registry = registry or Registry.from_yaml(settings.models_file)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(health.router)
    app.include_router(models.router)
    return app


app = create_app()

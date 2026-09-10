from fastapi import FastAPI

from app.config import settings
from app.logging import RequestIdMiddleware, configure_logging
from app.routes import health


def create_app() -> FastAPI:
    configure_logging(settings.log_level)
    app = FastAPI(title="OpenModel API")
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    return app


app = create_app()

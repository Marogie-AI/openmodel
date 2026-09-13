import logging
import time
from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"


def configure_logging(level: str) -> None:
    """Emit JSON logs from both structlog and stdlib logging."""
    level_no = logging.getLevelNamesMapping()[level.upper()]
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level_no)

    # uvicorn ships its own handlers; drop them so every line lands on the JSON root handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # --no-access-log only clears uvicorn's own handlers and propagate, which the loop above
    # restores; the level is what actually keeps its access lines out, now `request` replaces them.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


log = structlog.get_logger()

SILENT_PATHS = frozenset({"/health", "/ready", "/metrics"})


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Bind a request id into the structlog context and echo it back."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            if request.url.path not in SILENT_PATHS:
                log.info(
                    "request",
                    method=request.method,
                    path=request.url.path,
                    status=response.status_code,
                    duration_ms=round((time.perf_counter() - start) * 1000, 1),
                )
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[REQUEST_ID_HEADER] = request_id
        rate = getattr(request.state, "rate", None)
        if rate is not None:
            # setdefault: a 429 handler has already written the limit that was actually hit.
            response.headers.setdefault("X-RateLimit-Limit", str(rate.limit))
            response.headers.setdefault("X-RateLimit-Remaining", str(rate.remaining))
        return response

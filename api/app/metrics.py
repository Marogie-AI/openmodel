import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from prometheus_client import Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.schemas.backend import Usage

http_requests_total = Counter(
    "http_requests_total", "HTTP requests served.", ["method", "path", "status"]
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request duration.", ["method", "path"]
)
llm_tokens_total = Counter("llm_tokens_total", "Tokens processed.", ["direction", "model"])
llm_inflight = Gauge("llm_inflight", "LLM requests in flight.", ["model"])
llm_ttft_seconds = Histogram("llm_ttft_seconds", "Time to first token.", ["model"])
llm_requests_total = Counter(
    "llm_requests_total", "LLM requests completed.", ["model", "endpoint", "status"]
)


class Tracked:
    """Handle from `track`, so a caller that swallows its own error can still report it."""

    def __init__(self) -> None:
        self.status = "ok"

    def failed(self) -> None:
        self.status = "error"


@asynccontextmanager
async def track(model: str, endpoint: str) -> AsyncIterator[Tracked]:
    """Count one LLM request: inflight while it runs, ok/error when it finishes."""
    tracked = Tracked()
    llm_inflight.labels(model).inc()
    try:
        yield tracked
    except BaseException:
        tracked.failed()
        raise
    finally:
        llm_inflight.labels(model).dec()
        llm_requests_total.labels(model, endpoint, tracked.status).inc()


def record_usage(model: str, usage: Usage) -> None:
    llm_tokens_total.labels("prompt", model).inc(usage.prompt_tokens)
    llm_tokens_total.labels("completion", model).inc(usage.completion_tokens)


def observe_ttft(model: str, seconds: float) -> None:
    llm_ttft_seconds.labels(model).observe(seconds)


class MetricsMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so streaming responses are not buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return

        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Starlette sets scope["route"] during routing; absent means nothing matched.
            path = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"]
            http_requests_total.labels(method, path, str(status)).inc()
            http_request_duration_seconds.labels(method, path).observe(time.perf_counter() - start)

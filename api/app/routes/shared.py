import contextlib
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable

from fastapi import Request

from app.backends.ollama import OllamaBackend
from app.errors import ApiError
from app.metrics import observe_ttft, record_usage, track
from app.router import Capability, Registry
from app.schemas.backend import ChatDelta
from app.sse import DONE, error_event

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# Builds one SSE event: the final delta when the stream is done, otherwise a content-only chunk.
ChunkFactory = Callable[[str, ChatDelta | None], bytes]


def options_from(
    temperature: float | None, top_p: float | None, max_tokens: int | None
) -> dict[str, float | int]:
    pairs: dict[str, float | int | None] = {
        "temperature": temperature,
        "top_p": top_p,
        "num_predict": max_tokens,
    }
    return {key: value for key, value in pairs.items() if value is not None}


def backend_for(request: Request, model: str, capability: Capability) -> OllamaBackend:
    registry: Registry = request.app.state.registry
    spec = registry.require(model, capability)
    backends: dict[str, OllamaBackend] = request.app.state.backends
    return backends[spec.backend_url]


async def stream_events(
    request: Request,
    deltas: AsyncGenerator[ChatDelta, None],
    chunk: ChunkFactory,
    model: str,
    endpoint: str,
    prelude: bytes | None = None,
) -> AsyncIterator[bytes]:
    if prelude is not None:
        yield prelude
    async with track(model, endpoint) as tracked:
        start = time.perf_counter()
        first_token = True
        try:
            # aclosing: a disconnect closes the backend generator, cancelling the upstream call.
            async with contextlib.aclosing(deltas) as stream:
                async for delta in stream:
                    if await request.is_disconnected():
                        return
                    if delta.content:
                        if first_token:
                            observe_ttft(model, time.perf_counter() - start)
                            first_token = False
                        yield chunk(delta.content, None)
                    if delta.done:
                        if delta.usage is not None:
                            record_usage(model, delta.usage)
                        yield chunk("", delta)
        except ApiError as exc:
            # The error is delivered in-stream rather than raised, so count it here.
            tracked.failed()
            yield error_event(exc)
    yield DONE

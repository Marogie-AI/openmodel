import contextlib
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from uuid import UUID

from fastapi import Request

from app.auth.principal import Principal
from app.backends.ollama import OllamaBackend
from app.errors import ApiError, ModelNotAllowed
from app.metrics import observe_ttft, record_usage, track
from app.ratelimit import ConcurrencySlot
from app.router import Capability, Registry
from app.schemas.backend import ChatDelta, Usage
from app.sse import DONE, error_event
from app.usage import record_for, schedule

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


def backend_for(
    request: Request, model: str, capability: Capability, principal: Principal
) -> OllamaBackend:
    registry: Registry = request.app.state.registry
    # Order matters: an unknown model is a 404 and a capability mismatch a 400, whatever the plan.
    spec = registry.require(model, capability)
    if model not in principal.models:
        raise ModelNotAllowed(model)
    backends: dict[str, OllamaBackend] = request.app.state.backends
    return backends[spec.backend_url]


async def stream_events(
    request: Request,
    deltas: AsyncGenerator[ChatDelta, None],
    chunk: ChunkFactory,
    model: str,
    endpoint: str,
    api_key_id: UUID,
    prelude: bytes | None = None,
    slot: ConcurrencySlot | None = None,
) -> AsyncIterator[bytes]:
    """Drive one SSE response. `slot`, if given, is already acquired and is released here."""
    async with track(model, endpoint) as tracked:
        start = time.perf_counter()
        first_token = True
        # A generator abandoned before the stream ends is a client that hung up: 499.
        status = 499
        usage: Usage | None = None
        try:
            if prelude is not None:
                yield prelude
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
                            usage = delta.usage
                        yield chunk("", delta)
            status = 200
        except ApiError as exc:
            # The error is delivered in-stream rather than raised, so count it here.
            status = exc.status_code
            tracked.failed()
            yield error_event(exc)
        finally:
            schedule(request, record_for(api_key_id, model, endpoint, start, status, usage))
            # Completion, disconnect, error or an abandoned generator: the slot goes back.
            if slot is not None:
                await slot.release()
    yield DONE

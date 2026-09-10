import contextlib
import json
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.backends.ollama import OllamaBackend
from app.errors import ApiError
from app.router import Registry
from app.schemas.backend import ChatResult
from app.schemas.chat import (
    ChatChoice,
    ChatCompletion,
    ChatCompletionChunk,
    ChatMessage,
    ChatRequest,
    ChunkChoice,
    UsageOut,
)

router = APIRouter()

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _options(body: ChatRequest) -> dict[str, float | int]:
    pairs: dict[str, float | int | None] = {
        "temperature": body.temperature,
        "top_p": body.top_p,
        "num_predict": body.max_tokens,
    }
    return {key: value for key, value in pairs.items() if value is not None}


def _backend(request: Request, model: str) -> OllamaBackend:
    registry: Registry = request.app.state.registry
    spec = registry.require(model, "chat")
    backends: dict[str, OllamaBackend] = request.app.state.backends
    return backends[spec.backend_url]


def _event(payload: str) -> str:
    return f"data: {payload}\n\n"


async def _stream_events(
    request: Request,
    backend: OllamaBackend,
    body: ChatRequest,
    messages: list[dict[str, str]],
    completion_id: str,
    created: int,
) -> AsyncIterator[str]:
    def chunk(
        delta: dict[str, str],
        finish_reason: str | None = None,
        usage: UsageOut | None = None,
    ) -> str:
        return _event(
            ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=body.model,
                choices=[ChunkChoice(delta=delta, finish_reason=finish_reason)],
                usage=usage,
            ).model_dump_json(exclude_none=True)
        )

    yield chunk({"role": "assistant", "content": ""})
    try:
        stream = backend.chat_stream(body.model, messages, _options(body))
        # aclosing: a disconnect must close the backend generator, cancelling the upstream request.
        async with contextlib.aclosing(stream) as deltas:
            async for delta in deltas:
                if await request.is_disconnected():
                    return
                if delta.content:
                    yield chunk({"content": delta.content})
                if delta.done:
                    yield chunk(
                        {},
                        finish_reason=delta.finish_reason,
                        usage=UsageOut.of(delta.usage) if delta.usage else None,
                    )
    except ApiError as exc:
        error = {"message": exc.message, "type": exc.type, "code": exc.code}
        yield _event(json.dumps({"error": error}))
    yield _event("[DONE]")


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request, body: ChatRequest
) -> ChatCompletion | StreamingResponse:
    backend = _backend(request, body.model)
    messages = [message.model_dump() for message in body.messages]
    completion_id = "chatcmpl-" + uuid4().hex[:24]
    created = int(time.time())

    if body.stream:
        return StreamingResponse(
            _stream_events(request, backend, body, messages, completion_id, created),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
        )

    result: ChatResult = await backend.chat(body.model, messages, _options(body))
    return ChatCompletion(
        id=completion_id,
        created=created,
        model=body.model,
        choices=[
            ChatChoice(
                message=ChatMessage(role="assistant", content=result.content),
                finish_reason=result.finish_reason,
            )
        ],
        usage=UsageOut.of(result.usage),
    )

import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth.principal import Principal
from app.metrics import observe_ttft, record_usage, track
from app.ratelimit import enforce, slot_for
from app.routes.shared import STREAM_HEADERS, backend_for, options_from, stream_events
from app.schemas.backend import ChatDelta, ChatResult
from app.schemas.chat import (
    ChatChoice,
    ChatCompletion,
    ChatCompletionChunk,
    ChatMessage,
    ChatRequest,
    ChunkChoice,
    UsageOut,
)
from app.sse import sse

router = APIRouter()


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request, body: ChatRequest, principal: Annotated[Principal, Depends(enforce)]
) -> ChatCompletion | StreamingResponse:
    backend = backend_for(request, body.model, "chat", principal)
    messages = [message.model_dump() for message in body.messages]
    options = options_from(body.temperature, body.top_p, body.max_tokens)
    completion_id = "chatcmpl-" + uuid4().hex[:24]
    created = int(time.time())

    def chunk(
        delta: dict[str, str],
        finish_reason: str | None = None,
        usage: UsageOut | None = None,
    ) -> bytes:
        return sse(
            ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=body.model,
                choices=[ChunkChoice(delta=delta, finish_reason=finish_reason)],
                usage=usage,
            ).model_dump_json(exclude_none=True)
        )

    if body.stream:

        def event(content: str, final: ChatDelta | None) -> bytes:
            if final is None:
                return chunk({"content": content})
            return chunk({}, final.finish_reason, UsageOut.of(final.usage) if final.usage else None)

        return StreamingResponse(
            stream_events(
                request,
                backend.chat_stream(body.model, messages, options),
                event,
                body.model,
                "chat",
                prelude=chunk({"role": "assistant", "content": ""}),
                slot=slot_for(request, principal),
            ),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
        )

    async with slot_for(request, principal), track(body.model, "chat"):
        start = time.perf_counter()
        result: ChatResult = await backend.chat(body.model, messages, options)
        observe_ttft(body.model, time.perf_counter() - start)
        record_usage(body.model, result.usage)
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

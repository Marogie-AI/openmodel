import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth.principal import Principal
from app.errors import PromptListUnsupported
from app.metrics import observe_ttft, record_usage, track
from app.ratelimit import ConcurrencySlot, enforce, slot_for
from app.routes.shared import STREAM_HEADERS, backend_for, options_from, stream_events
from app.schemas.backend import ChatDelta, ChatResult
from app.schemas.chat import UsageOut
from app.schemas.completions import Completion, CompletionChoice, CompletionRequest
from app.sse import sse

router = APIRouter()


def _prompt(prompt: str | list[str]) -> str:
    if isinstance(prompt, str):
        return prompt
    if len(prompt) > 1:
        raise PromptListUnsupported()
    return prompt[0]


@router.post("/v1/completions", response_model=None)
async def completions(
    request: Request,
    body: CompletionRequest,
    principal: Annotated[Principal, Depends(enforce)],
    slot: Annotated[ConcurrencySlot, Depends(slot_for)],
) -> Completion | StreamingResponse:
    backend = backend_for(request, body.model, "completion", principal)
    prompt = _prompt(body.prompt)
    options = options_from(body.temperature, body.top_p, body.max_tokens)
    completion_id = "cmpl-" + uuid4().hex[:24]
    created = int(time.time())

    def completion(choice: CompletionChoice, usage: UsageOut | None) -> Completion:
        return Completion(
            id=completion_id, created=created, model=body.model, choices=[choice], usage=usage
        )

    if body.stream:
        # Acquired here, not inside the generator: an overflow must be a 429 before headers.
        await slot.acquire()

        def event(content: str, final: ChatDelta | None) -> bytes:
            choice = CompletionChoice(
                text=content, finish_reason=final.finish_reason if final else None
            )
            usage = UsageOut.of(final.usage) if final and final.usage else None
            return sse(completion(choice, usage).model_dump_json(exclude_none=True))

        return StreamingResponse(
            stream_events(
                request,
                backend.generate_stream(body.model, prompt, options),
                event,
                body.model,
                "completion",
                slot=slot,
            ),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
        )

    async with slot, track(body.model, "completion"):
        start = time.perf_counter()
        result: ChatResult = await backend.generate(body.model, prompt, options)
        observe_ttft(body.model, time.perf_counter() - start)
        record_usage(body.model, result.usage)
    return completion(
        CompletionChoice(text=result.content, finish_reason=result.finish_reason),
        UsageOut.of(result.usage),
    )

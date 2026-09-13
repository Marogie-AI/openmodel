import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.auth.principal import Principal
from app.errors import ApiError
from app.metrics import observe_ttft, record_usage, track
from app.ratelimit import ConcurrencySlot, enforce, slot_for
from app.routes.shared import backend_for
from app.schemas.embeddings import Embedding, EmbeddingList, EmbeddingRequest, EmbeddingUsage
from app.usage import record_for, schedule

router = APIRouter()


@router.post("/v1/embeddings")
async def embeddings(
    request: Request,
    body: EmbeddingRequest,
    principal: Annotated[Principal, Depends(enforce)],
    slot: Annotated[ConcurrencySlot, Depends(slot_for)],
) -> EmbeddingList:
    entered = time.perf_counter()
    backend = backend_for(request, body.model, "embedding", principal)
    inputs = [body.input] if isinstance(body.input, str) else body.input
    async with slot, track(body.model, "embedding"):
        start = time.perf_counter()
        try:
            result = await backend.embed(body.model, inputs)
        except ApiError as exc:
            schedule(
                request,
                record_for(principal.api_key_id, body.model, "embedding", entered, exc.status_code),
            )
            raise
        observe_ttft(body.model, time.perf_counter() - start)
        record_usage(body.model, result.usage)
        schedule(
            request,
            record_for(principal.api_key_id, body.model, "embedding", entered, 200, result.usage),
        )
    tokens = result.usage.prompt_tokens
    return EmbeddingList(
        data=[
            Embedding(index=index, embedding=vector)
            for index, vector in enumerate(result.embeddings)
        ],
        model=body.model,
        usage=EmbeddingUsage(prompt_tokens=tokens, total_tokens=tokens),
    )

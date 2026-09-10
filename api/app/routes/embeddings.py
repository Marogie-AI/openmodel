import time

from fastapi import APIRouter, Request

from app.metrics import observe_ttft, record_usage, track
from app.routes.shared import backend_for
from app.schemas.embeddings import Embedding, EmbeddingList, EmbeddingRequest, EmbeddingUsage

router = APIRouter()


@router.post("/v1/embeddings")
async def embeddings(request: Request, body: EmbeddingRequest) -> EmbeddingList:
    backend = backend_for(request, body.model, "embedding")
    inputs = [body.input] if isinstance(body.input, str) else body.input
    async with track(body.model, "embedding"):
        start = time.perf_counter()
        result = await backend.embed(body.model, inputs)
        observe_ttft(body.model, time.perf_counter() - start)
        record_usage(body.model, result.usage)
    tokens = result.usage.prompt_tokens
    return EmbeddingList(
        data=[
            Embedding(index=index, embedding=vector)
            for index, vector in enumerate(result.embeddings)
        ],
        model=body.model,
        usage=EmbeddingUsage(prompt_tokens=tokens, total_tokens=tokens),
    )

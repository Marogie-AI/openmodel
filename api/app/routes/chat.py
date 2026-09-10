import time
from uuid import uuid4

from fastapi import APIRouter, Request

from app.backends.ollama import OllamaBackend
from app.router import Registry
from app.schemas.backend import ChatResult
from app.schemas.chat import ChatChoice, ChatCompletion, ChatMessage, ChatRequest, UsageOut

router = APIRouter()


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


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest) -> ChatCompletion:
    backend = _backend(request, body.model)
    messages = [message.model_dump() for message in body.messages]
    result: ChatResult = await backend.chat(body.model, messages, _options(body))
    return ChatCompletion(
        id="chatcmpl-" + uuid4().hex[:24],
        created=int(time.time()),
        model=body.model,
        choices=[
            ChatChoice(
                message=ChatMessage(role="assistant", content=result.content),
                finish_reason=result.finish_reason,
            )
        ],
        usage=UsageOut.of(result.usage),
    )

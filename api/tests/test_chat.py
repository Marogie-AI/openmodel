import json
from typing import Any

import httpx
import respx
from httpx import AsyncClient

from tests.conftest import BACKEND_URL

CHAT_URL = f"{BACKEND_URL}/api/chat"

BODY: dict[str, Any] = {
    "model": "qwen2.5:0.5b",
    "messages": [{"role": "user", "content": "yo"}],
}


def ollama_response(**overrides: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "message": {"role": "assistant", "content": "hi there"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 7,
        "eval_count": 3,
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


@respx.mock
async def test_sync_returns_openai_completion(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())

    response = await client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("chatcmpl-")
    assert payload["object"] == "chat.completion"
    assert payload["created"] > 0
    assert payload["model"] == "qwen2.5:0.5b"
    assert payload["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi there"},
            "finish_reason": "stop",
        }
    ]
    assert payload["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}


@respx.mock
async def test_sampling_options_map_to_ollama(client: AsyncClient) -> None:
    route = respx.post(CHAT_URL).mock(return_value=ollama_response())

    response = await client.post(
        "/v1/chat/completions", json={**BODY, "max_tokens": 16, "temperature": 0.25}
    )

    assert response.status_code == 200
    assert json.loads(route.calls[0].request.content) == {
        "model": "qwen2.5:0.5b",
        "messages": [{"role": "user", "content": "yo"}],
        "stream": False,
        "options": {"temperature": 0.25, "num_predict": 16},
    }


async def test_unknown_model_returns_404_envelope(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json={**BODY, "model": "ghost"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "The model 'ghost' does not exist.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }


async def test_embedding_model_returns_400_capability(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json={**BODY, "model": "nomic-embed-text"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_capability"


async def test_empty_messages_is_422(client: AsyncClient) -> None:
    response = await client.post("/v1/chat/completions", json={**BODY, "messages": []})

    assert response.status_code == 422


@respx.mock
async def test_backend_500_becomes_502_envelope(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    response = await client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "backend_unavailable"

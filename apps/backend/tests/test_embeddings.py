import json
from typing import Any

import httpx
import respx
from httpx import AsyncClient

from tests.conftest import BACKEND_URL

EMBED_URL = f"{BACKEND_URL}/api/embed"

BODY: dict[str, Any] = {"model": "nomic-embed-text", "input": "hello"}


def ollama_response(*vectors: list[float], prompt_eval_count: int = 5) -> httpx.Response:
    return httpx.Response(
        200, json={"embeddings": list(vectors), "prompt_eval_count": prompt_eval_count}
    )


@respx.mock
async def test_string_input_returns_one_embedding(client: AsyncClient) -> None:
    route = respx.post(EMBED_URL).mock(return_value=ollama_response([0.1, 0.2]))

    response = await client.post("/v1/embeddings", json=BODY)

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "model": "nomic-embed-text",
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }
    assert json.loads(route.calls[0].request.content) == {
        "model": "nomic-embed-text",
        "input": ["hello"],
    }


@respx.mock
async def test_list_input_returns_indexed_embeddings(client: AsyncClient) -> None:
    route = respx.post(EMBED_URL).mock(return_value=ollama_response([0.1], [0.2], [0.3]))

    response = await client.post("/v1/embeddings", json={**BODY, "input": ["a", "b", "c"]})

    assert response.status_code == 200
    assert [item["index"] for item in response.json()["data"]] == [0, 1, 2]
    assert [item["embedding"] for item in response.json()["data"]] == [[0.1], [0.2], [0.3]]
    assert json.loads(route.calls[0].request.content)["input"] == ["a", "b", "c"]


async def test_empty_input_list_is_422(client: AsyncClient) -> None:
    response = await client.post("/v1/embeddings", json={**BODY, "input": []})

    assert response.status_code == 422


async def test_chat_model_returns_400_capability(client: AsyncClient) -> None:
    response = await client.post("/v1/embeddings", json={**BODY, "model": "qwen2.5:0.5b"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_capability"


async def test_unknown_model_returns_404_envelope(client: AsyncClient) -> None:
    response = await client.post("/v1/embeddings", json={**BODY, "model": "ghost"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


@respx.mock
async def test_backend_500_becomes_502_envelope(client: AsyncClient) -> None:
    respx.post(EMBED_URL).mock(return_value=httpx.Response(500))

    response = await client.post("/v1/embeddings", json=BODY)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "backend_unavailable"

import json
from typing import Any

import httpx
import respx
from httpx import AsyncClient

from tests.conftest import BACKEND_URL

GENERATE_URL = f"{BACKEND_URL}/api/generate"

BODY: dict[str, Any] = {"model": "qwen2.5:0.5b", "prompt": "count to three"}


def ollama_response(**overrides: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "response": "one two three",
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 4,
        "eval_count": 3,
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


def ndjson(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


async def sse_data(client: AsyncClient, body: dict[str, Any]) -> list[str]:
    async with client.stream("POST", "/v1/completions", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return [
            line.removeprefix("data: ")
            async for line in response.aiter_lines()
            if line.startswith("data: ")
        ]


@respx.mock
async def test_sync_returns_openai_completion(client: AsyncClient) -> None:
    route = respx.post(GENERATE_URL).mock(return_value=ollama_response())

    response = await client.post("/v1/completions", json={**BODY, "max_tokens": 16})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("cmpl-")
    assert payload["object"] == "text_completion"
    assert payload["created"] > 0
    assert payload["model"] == "qwen2.5:0.5b"
    assert payload["choices"] == [{"index": 0, "text": "one two three", "finish_reason": "stop"}]
    assert payload["usage"] == {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}
    assert json.loads(route.calls[0].request.content) == {
        "model": "qwen2.5:0.5b",
        "prompt": "count to three",
        "stream": False,
        "options": {"num_predict": 16},
    }


@respx.mock
async def test_single_item_prompt_list_is_accepted(client: AsyncClient) -> None:
    route = respx.post(GENERATE_URL).mock(return_value=ollama_response())

    response = await client.post("/v1/completions", json={**BODY, "prompt": ["only one"]})

    assert response.status_code == 200
    assert json.loads(route.calls[0].request.content)["prompt"] == "only one"


async def test_multi_prompt_list_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/v1/completions", json={**BODY, "prompt": ["a", "b"]})

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Batched prompts are not supported; send a single prompt.",
            "type": "invalid_request_error",
            "code": "prompt_list_unsupported",
        }
    }


async def test_empty_prompt_list_is_422(client: AsyncClient) -> None:
    response = await client.post("/v1/completions", json={**BODY, "prompt": []})

    assert response.status_code == 422


async def test_embedding_model_returns_400_capability(client: AsyncClient) -> None:
    response = await client.post("/v1/completions", json={**BODY, "model": "nomic-embed-text"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_capability"


async def test_unknown_model_returns_404_envelope(client: AsyncClient) -> None:
    response = await client.post("/v1/completions", json={**BODY, "model": "ghost"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


@respx.mock
async def test_stream_emits_text_chunks_usage_then_done(client: AsyncClient) -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            200,
            text=ndjson(
                {"response": "one", "done": False},
                {"response": " two", "done": False},
                {
                    "response": "",
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 4,
                    "eval_count": 3,
                },
            ),
        )
    )

    events = await sse_data(client, {**BODY, "stream": True})

    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert all(chunk["object"] == "text_completion" for chunk in chunks)
    assert all(chunk["id"] == chunks[0]["id"] for chunk in chunks)
    assert [chunk["choices"][0]["text"] for chunk in chunks] == ["one", " two", ""]
    assert chunks[0]["choices"][0]["index"] == 0
    assert chunks[0]["choices"][0].get("finish_reason") is None
    assert "usage" not in chunks[0]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}


@respx.mock
async def test_stream_backend_failure_emits_error_event_then_done(client: AsyncClient) -> None:
    respx.post(GENERATE_URL).mock(return_value=httpx.Response(500))

    events = await sse_data(client, {**BODY, "stream": True})

    assert json.loads(events[0])["error"]["code"] == "backend_unavailable"
    assert events[-1] == "[DONE]"

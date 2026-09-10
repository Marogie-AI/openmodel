import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import Request
from httpx import AsyncClient

from app.backends.ollama import OllamaBackend
from app.schemas.backend import ChatDelta, Usage
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


def ndjson(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


async def sse_data(client: AsyncClient, body: dict[str, Any]) -> list[str]:
    async with client.stream("POST", "/v1/chat/completions", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        return [
            line.removeprefix("data: ")
            async for line in response.aiter_lines()
            if line.startswith("data: ")
        ]


@respx.mock
async def test_stream_emits_role_content_usage_then_done(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            text=ndjson(
                {"message": {"content": "hi"}, "done": False},
                {"message": {"content": " there"}, "done": False},
                {
                    "message": {"content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 7,
                    "eval_count": 3,
                },
            ),
        )
    )

    events = await sse_data(client, {**BODY, "stream": True})

    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert all(chunk["id"] == chunks[0]["id"] for chunk in chunks)
    assert [chunk["choices"][0]["delta"] for chunk in chunks] == [
        {"role": "assistant", "content": ""},
        {"content": "hi"},
        {"content": " there"},
        {},
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert "usage" not in chunks[0]
    assert chunks[0]["choices"][0].get("finish_reason") is None


@respx.mock
async def test_stream_request_body_asks_ollama_to_stream(client: AsyncClient) -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text=ndjson({"message": {"content": "x"}, "done": True}))
    )

    await sse_data(client, {**BODY, "stream": True, "max_tokens": 8})

    assert json.loads(route.calls[0].request.content) == {
        "model": "qwen2.5:0.5b",
        "messages": [{"role": "user", "content": "yo"}],
        "stream": True,
        "options": {"num_predict": 8},
    }


@respx.mock
async def test_stream_backend_failure_emits_error_event_then_done(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    events = await sse_data(client, {**BODY, "stream": True})

    assert json.loads(events[0])["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert json.loads(events[1]) == {
        "error": {
            "message": "Backend returned HTTP 500.",
            "type": "server_error",
            "code": "backend_unavailable",
        }
    }
    assert events[-1] == "[DONE]"


@respx.mock
async def test_stream_emits_content_carried_on_the_done_line(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            text=ndjson(
                {
                    "message": {"content": "x"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                }
            ),
        )
    )

    events = await sse_data(client, {**BODY, "stream": True})

    chunks = [json.loads(event) for event in events[:-1]]
    assert [chunk["choices"][0]["delta"] for chunk in chunks] == [
        {"role": "assistant", "content": ""},
        {"content": "x"},
        {},
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 2
    assert events[-1] == "[DONE]"


async def _resolved(value: bool) -> bool:
    return value


async def test_stream_disconnect_stops_and_closes_the_backend_stream(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumed: list[str] = []
    closed = False

    async def deltas(*_args: Any, **_kwargs: Any) -> AsyncIterator[ChatDelta]:
        nonlocal closed
        try:
            for text in ("a", "b"):
                consumed.append(text)
                yield ChatDelta(content=text, done=False)
            yield ChatDelta(
                content="", done=True, usage=Usage(prompt_tokens=1, completion_tokens=1)
            )
        finally:
            closed = True

    disconnects = iter([False, True])
    monkeypatch.setattr(OllamaBackend, "chat_stream", deltas)
    monkeypatch.setattr(Request, "is_disconnected", lambda _self: _resolved(next(disconnects)))

    events = await sse_data(client, {**BODY, "stream": True})

    assert [json.loads(event)["choices"][0]["delta"] for event in events] == [
        {"role": "assistant", "content": ""},
        {"content": "a"},
    ]
    assert "[DONE]" not in events
    assert consumed == ["a", "b"]
    assert closed

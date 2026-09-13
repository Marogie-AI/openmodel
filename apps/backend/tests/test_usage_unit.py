"""What the routes hand to the usage sink, with no database in sight."""

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
from app.usage import UsageRecord
from tests.conftest import BACKEND_URL, TEST_PRINCIPAL

CHAT_URL = f"{BACKEND_URL}/api/chat"
EMBED_URL = f"{BACKEND_URL}/api/embed"

BODY: dict[str, Any] = {
    "model": "qwen2.5:0.5b",
    "messages": [{"role": "user", "content": "yo"}],
}


def ndjson(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


async def drain(client: AsyncClient, body: dict[str, Any]) -> None:
    async with client.stream("POST", "/v1/chat/completions", json=body) as response:
        async for _ in response.aiter_lines():
            pass


def only(records: list[UsageRecord]) -> UsageRecord:
    assert len(records) == 1
    return records[0]


@respx.mock
async def test_sync_chat_records_tokens_and_200(
    client: AsyncClient, usage_records: list[UsageRecord]
) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "hi"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )
    )

    await client.post("/v1/chat/completions", json=BODY)

    record = only(usage_records)
    assert record.api_key_id == TEST_PRINCIPAL.api_key_id
    assert record.model_name == "qwen2.5:0.5b"
    assert record.endpoint == "chat"
    assert record.prompt_tokens == 7
    assert record.completion_tokens == 3
    assert record.status_code == 200
    assert record.latency_ms >= 0


@respx.mock
async def test_sync_backend_failure_records_502_with_zero_tokens(
    client: AsyncClient, usage_records: list[UsageRecord]
) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    response = await client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 502
    record = only(usage_records)
    assert record.status_code == 502
    assert (record.prompt_tokens, record.completion_tokens) == (0, 0)


@respx.mock
async def test_embeddings_record_their_endpoint(
    client: AsyncClient, usage_records: list[UsageRecord]
) -> None:
    respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 4})
    )

    await client.post("/v1/embeddings", json={"model": "nomic-embed-text", "input": "hi"})

    record = only(usage_records)
    assert record.endpoint == "embedding"
    assert record.model_name == "nomic-embed-text"
    assert (record.prompt_tokens, record.status_code) == (4, 200)


@respx.mock
async def test_stream_records_tokens_from_the_final_delta(
    client: AsyncClient, usage_records: list[UsageRecord]
) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            text=ndjson(
                {"message": {"content": "hi"}, "done": False},
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

    await drain(client, {**BODY, "stream": True})

    record = only(usage_records)
    assert record.status_code == 200
    assert (record.prompt_tokens, record.completion_tokens) == (7, 3)


@respx.mock
async def test_in_stream_error_records_its_status_with_zero_tokens(
    client: AsyncClient, usage_records: list[UsageRecord]
) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    await drain(client, {**BODY, "stream": True})

    record = only(usage_records)
    assert record.status_code == 502
    assert (record.prompt_tokens, record.completion_tokens) == (0, 0)


async def _resolved(value: bool) -> bool:
    return value


async def test_disconnect_records_499(
    client: AsyncClient, usage_records: list[UsageRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def deltas(*_args: Any, **_kwargs: Any) -> AsyncIterator[ChatDelta]:
        yield ChatDelta(content="a", done=False)
        yield ChatDelta(content="", done=True, usage=Usage(prompt_tokens=1, completion_tokens=1))

    disconnects = iter([False, True])
    monkeypatch.setattr(OllamaBackend, "chat_stream", deltas)
    monkeypatch.setattr(Request, "is_disconnected", lambda _self: _resolved(next(disconnects)))

    await drain(client, {**BODY, "stream": True})

    record = only(usage_records)
    assert record.status_code == 499
    assert (record.prompt_tokens, record.completion_tokens) == (0, 0)

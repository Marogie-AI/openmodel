"""Usage metering end to end: rows written in the background, read back through /api/usage."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey, Request
from app.usage import schedule_write
from tests.conftest import ADMIN_HEADERS, BACKEND_URL, client_for

pytestmark = pytest.mark.db

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


def ndjson(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


async def flush(app: FastAPI) -> None:
    """Metering writes in the background, so wait for the tasks the calls just scheduled."""
    await asyncio.gather(*app.state.usage_tasks)


async def rows(session: AsyncSession) -> list[Request]:
    result = await session.scalars(select(Request).order_by(Request.id))
    return list(result.all())


@respx.mock
async def test_sync_chat_writes_one_row(
    db_app: FastAPI, auth_client: AsyncClient, session: AsyncSession
) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())

    response = await auth_client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    await flush(db_app)

    (row,) = await rows(session)
    key = await session.scalar(select(ApiKey))
    assert key is not None
    assert row.api_key_id == key.id
    assert row.model_name == "qwen2.5:0.5b"
    assert row.endpoint == "chat"
    assert (row.prompt_tokens, row.completion_tokens) == (7, 3)
    assert row.status_code == 200
    assert row.latency_ms >= 0


@respx.mock
async def test_backend_failure_writes_502_with_zero_tokens(
    db_app: FastAPI, auth_client: AsyncClient, session: AsyncSession
) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))

    response = await auth_client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 502
    await flush(db_app)

    (row,) = await rows(session)
    assert row.status_code == 502
    assert (row.prompt_tokens, row.completion_tokens) == (0, 0)


@respx.mock
async def test_stream_writes_tokens_from_the_final_chunk(
    db_app: FastAPI, auth_client: AsyncClient, session: AsyncSession
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
                    "prompt_eval_count": 11,
                    "eval_count": 5,
                },
            ),
        )
    )

    body = {**BODY, "stream": True}
    async with auth_client.stream("POST", "/v1/chat/completions", json=body) as response:
        assert response.status_code == 200
        async for _ in response.aiter_lines():
            pass
    await flush(db_app)

    (row,) = await rows(session)
    assert (row.prompt_tokens, row.completion_tokens) == (11, 5)
    assert row.status_code == 200


@respx.mock
async def test_a_broken_writer_does_not_affect_the_response(
    db_app: FastAPI, auth_client: AsyncClient, session: AsyncSession
) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())

    def exploding() -> AsyncSession:
        raise RuntimeError("no database today")

    db_app.state.usage_sink = partial(schedule_write, exploding, db_app.state.usage_tasks)

    response = await auth_client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    await flush(db_app)
    assert await rows(session) == []


@respx.mock
async def test_usage_totals_and_groups(
    db_app: FastAPI, auth_client: AsyncClient, db_client: AsyncClient
) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())
    await auth_client.post("/v1/chat/completions", json=BODY)
    await auth_client.post("/v1/chat/completions", json=BODY)
    await flush(db_app)

    summary = (await auth_client.get("/api/usage")).json()
    assert summary["requests"] == 2
    assert summary["prompt_tokens"] == 14
    assert summary["completion_tokens"] == 6
    assert summary["groups"] == []
    assert datetime.fromisoformat(summary["from"]) < datetime.fromisoformat(summary["to"])
    assert UUID(summary["organization_id"])

    by_model = (await auth_client.get("/api/usage", params={"group_by": "model"})).json()
    assert by_model["groups"] == [
        {"key": "qwen2.5:0.5b", "requests": 2, "prompt_tokens": 14, "completion_tokens": 6}
    ]

    by_user = (await auth_client.get("/api/usage", params={"group_by": "user"})).json()
    assert by_user["groups"][0]["key"] == "principal@example.com"

    by_day = (await auth_client.get("/api/usage", params={"group_by": "day"})).json()
    assert by_day["groups"][0]["key"] == datetime.now(UTC).date().isoformat()


@respx.mock
async def test_window_excludes_calls_outside_it(db_app: FastAPI, auth_client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())
    await auth_client.post("/v1/chat/completions", json=BODY)
    await flush(db_app)

    past = datetime.now(UTC) - timedelta(days=2)
    summary = (
        await auth_client.get(
            "/api/usage",
            params={"from": (past - timedelta(days=1)).isoformat(), "to": past.isoformat()},
        )
    ).json()

    assert summary["requests"] == 0
    assert summary["prompt_tokens"] == 0


@respx.mock
async def test_another_organization_sees_none_of_it(
    db_app: FastAPI, auth_client: AsyncClient, db_client: AsyncClient
) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())
    await auth_client.post("/v1/chat/completions", json=BODY)
    await flush(db_app)

    org = await db_client.post(
        "/admin/orgs", json={"name": "other", "plan_code": "free"}, headers=ADMIN_HEADERS
    )
    user = await db_client.post(
        "/admin/users",
        json={"organization_id": org.json()["id"], "email": "other@example.com"},
        headers=ADMIN_HEADERS,
    )
    key = await db_client.post(f"/admin/users/{user.json()['id']}/keys", headers=ADMIN_HEADERS)

    async with client_for(db_app, {"Authorization": f"Bearer {key.json()['key']}"}) as other:
        summary = (await other.get("/api/usage")).json()

    assert summary["requests"] == 0
    assert summary["organization_id"] == org.json()["id"]


async def test_unknown_group_by_is_a_422_envelope(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/usage", params={"group_by": "planet"})

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_usage_requires_a_principal(db_client: AsyncClient) -> None:
    assert (await db_client.get("/api/usage")).status_code == 401

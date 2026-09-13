import asyncio

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.metrics import llm_inflight, track
from tests.conftest import BACKEND_URL, TEST_REGISTRY
from tests.test_chat import BODY, CHAT_URL, ndjson, ollama_response, sse_data


async def sample(client: AsyncClient, series: str) -> float:
    """Value of one `name{labels}` series in the /metrics body, or 0.0 if not yet emitted."""
    body = (await client.get("/metrics")).text
    for line in body.splitlines():
        name, _, value = line.partition(" ")
        if name == series:
            return float(value)
    return 0.0


async def test_metrics_endpoint_serves_prometheus_text(client: AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text


@respx.mock
async def test_http_requests_counted_by_route_template(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())
    series = 'http_requests_total{method="POST",path="/v1/chat/completions",status="200"}'
    before = await sample(client, series)

    await client.post("/v1/chat/completions", json=BODY)

    assert await sample(client, series) == before + 1


async def test_unmatched_path_is_labelled_unmatched(client: AsyncClient) -> None:
    series = 'http_requests_total{method="GET",path="unmatched",status="404"}'
    before = await sample(client, series)

    await client.get("/nope")

    assert await sample(client, series) == before + 1


async def test_metrics_endpoint_does_not_count_itself(client: AsyncClient) -> None:
    series = 'http_requests_total{method="GET",path="/metrics",status="200"}'

    await client.get("/metrics")

    assert await sample(client, series) == 0.0


async def test_error_status_is_counted(client: AsyncClient) -> None:
    series = 'http_requests_total{method="POST",path="/v1/chat/completions",status="404"}'
    before = await sample(client, series)

    await client.post("/v1/chat/completions", json={**BODY, "model": "ghost"})

    assert await sample(client, series) == before + 1


@respx.mock
async def test_request_duration_is_observed(client: AsyncClient) -> None:
    respx.get(f"{BACKEND_URL}/api/tags").mock(return_value=httpx.Response(200))
    series = 'http_request_duration_seconds_count{method="GET",path="/health"}'
    before = await sample(client, series)

    await client.get("/health")

    assert await sample(client, series) == before + 1


@respx.mock
async def test_chat_records_tokens_inflight_and_status(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())
    prompt = 'llm_tokens_total{direction="prompt",model="qwen2.5:0.5b"}'
    completion = 'llm_tokens_total{direction="completion",model="qwen2.5:0.5b"}'
    ok = 'llm_requests_total{endpoint="chat",model="qwen2.5:0.5b",status="ok"}'
    before = (
        await sample(client, prompt),
        await sample(client, completion),
        await sample(client, ok),
    )

    await client.post("/v1/chat/completions", json=BODY)

    assert await sample(client, prompt) == before[0] + 7
    assert await sample(client, completion) == before[1] + 3
    assert await sample(client, ok) == before[2] + 1
    assert 'llm_inflight{model="qwen2.5:0.5b"} 0.0' in (await client.get("/metrics")).text


@respx.mock
async def test_chat_observes_ttft(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=ollama_response())
    series = 'llm_ttft_seconds_count{model="qwen2.5:0.5b"}'
    before = await sample(client, series)

    await client.post("/v1/chat/completions", json=BODY)

    assert await sample(client, series) == before + 1


async def test_unknown_model_records_no_llm_request(client: AsyncClient) -> None:
    error = 'llm_requests_total{endpoint="chat",model="ghost",status="error"}'

    response = await client.post("/v1/chat/completions", json={**BODY, "model": "ghost"})

    assert response.status_code == 404
    assert error not in (await client.get("/metrics")).text


@respx.mock
async def test_stream_failure_counts_as_error(client: AsyncClient) -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500))
    error = 'llm_requests_total{endpoint="chat",model="qwen2.5:0.5b",status="error"}'
    before = await sample(client, error)

    async with client.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}) as r:
        await r.aread()

    assert await sample(client, error) == before + 1


@respx.mock
async def test_stream_records_tokens_and_ttft(client: AsyncClient) -> None:
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
    prompt = 'llm_tokens_total{direction="prompt",model="qwen2.5:0.5b"}'
    ttft = 'llm_ttft_seconds_count{model="qwen2.5:0.5b"}'
    ok = 'llm_requests_total{endpoint="chat",model="qwen2.5:0.5b",status="ok"}'
    before = (await sample(client, prompt), await sample(client, ttft), await sample(client, ok))

    await sse_data(client, {**BODY, "stream": True})

    assert await sample(client, prompt) == before[0] + 7
    assert await sample(client, ttft) == before[1] + 1
    assert await sample(client, ok) == before[2] + 1


@respx.mock
async def test_embeddings_record_prompt_tokens(client: AsyncClient) -> None:
    respx.post(f"{BACKEND_URL}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 5})
    )
    prompt = 'llm_tokens_total{direction="prompt",model="nomic-embed-text"}'
    ok = 'llm_requests_total{endpoint="embedding",model="nomic-embed-text",status="ok"}'
    before = (await sample(client, prompt), await sample(client, ok))

    response = await client.post(
        "/v1/embeddings", json={"model": "nomic-embed-text", "input": "hello"}
    )

    assert response.status_code == 200
    assert await sample(client, prompt) == before[0] + 5
    assert await sample(client, ok) == before[1] + 1


async def test_client_cancellation_is_not_an_error(client: AsyncClient) -> None:
    error = 'llm_requests_total{endpoint="chat",model="qwen2.5:0.5b",status="error"}'
    ok = 'llm_requests_total{endpoint="chat",model="qwen2.5:0.5b",status="ok"}'
    before = (await sample(client, error), await sample(client, ok))

    with pytest.raises(asyncio.CancelledError):
        async with track("qwen2.5:0.5b", "chat"):
            raise asyncio.CancelledError

    assert await sample(client, error) == before[0]
    assert await sample(client, ok) == before[1] + 1
    assert 'llm_inflight{model="qwen2.5:0.5b"} 0.0' in (await client.get("/metrics")).text


async def test_inflight_series_exist_before_any_llm_call() -> None:
    """A pod must export `llm_inflight{model=...} 0` from startup, not on first request.

    prometheus_client emits no series at all for a labelled metric until a child
    exists, so without the pre-creation in `create_app` a freshly rolled pod is
    invisible to prometheus-adapter and the HPA reads `<unknown>`. Clearing first
    makes this fail if that loop is ever removed, whatever ran before it.
    """
    llm_inflight.clear()
    app = create_app(TEST_REGISTRY)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/metrics")).text

    for spec in TEST_REGISTRY.list():
        assert f'llm_inflight{{model="{spec.name}"}} 0.0' in body

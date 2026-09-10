import httpx
import respx
from httpx import AsyncClient

from tests.conftest import BACKEND_URL
from tests.test_chat import BODY, CHAT_URL, ollama_response


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

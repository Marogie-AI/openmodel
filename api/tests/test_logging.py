import io
import json
import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
import structlog
from httpx import AsyncClient

from app.logging import configure_logging


def parse(text: str) -> list[dict[str, Any]]:
    """Every emitted line, parsed as JSON."""
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines
    return [json.loads(line) for line in lines]


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Divert the JSON root handler into a buffer the test can read back."""
    stream = io.StringIO()
    handler = cast(logging.StreamHandler[Any], logging.getLogger().handlers[0])
    original = handler.setStream(stream)
    try:
        yield stream
    finally:
        if original is not None:
            handler.setStream(original)


def test_uvicorn_loggers_are_routed_through_structlog() -> None:
    configure_logging("INFO")
    stream = io.StringIO()
    cast(logging.StreamHandler[Any], logging.getLogger().handlers[0]).setStream(stream)

    logging.getLogger("uvicorn.error").info("x")
    structlog.get_logger().info("y")

    assert logging.getLogger("uvicorn.access").level == logging.WARNING

    payloads = parse(stream.getvalue())
    assert len(payloads) == 2
    for payload in payloads:
        assert payload["event"]
        assert payload["timestamp"]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate


async def test_request_event_carries_request_id(client: AsyncClient, captured: io.StringIO) -> None:
    await client.get("/v1/models")

    requests = [payload for payload in parse(captured.getvalue()) if payload["event"] == "request"]
    assert len(requests) == 1
    assert requests[0]["path"] == "/v1/models"
    assert requests[0]["status"] == 200
    assert requests[0]["request_id"]

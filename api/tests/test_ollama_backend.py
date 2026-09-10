import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from app.backends.ollama import OllamaBackend
from app.errors import BackendTimeout, BackendUnavailable
from app.schemas.backend import ChatDelta
from tests.conftest import BACKEND_URL


@pytest.fixture
async def backend() -> AsyncIterator[OllamaBackend]:
    async with httpx.AsyncClient() as client:
        yield OllamaBackend(client=client, base_url=BACKEND_URL, retries=2)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.backends.ollama.asyncio.sleep", instant)


class _FailingStream(httpx.AsyncByteStream):
    """Streams `chunks`, then raises `exc` — a connection dropped mid-generation."""

    def __init__(self, chunks: list[bytes], exc: Exception) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        raise self._exc


def ndjson(*lines: dict[str, Any]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


async def collect(stream: AsyncIterator[ChatDelta]) -> list[ChatDelta]:
    return [delta async for delta in stream]


@respx.mock
async def test_chat_maps_content_usage_and_finish_reason(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "hi"},
                "done_reason": "length",
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )
    )

    result = await backend.chat("m", [{"role": "user", "content": "yo"}], {"temperature": 0.5})

    assert result.content == "hi"
    assert result.usage.prompt_tokens == 7
    assert result.usage.completion_tokens == 3
    assert result.finish_reason == "length"
    assert json.loads(route.calls[0].request.content) == {
        "model": "m",
        "messages": [{"role": "user", "content": "yo"}],
        "stream": False,
        "options": {"temperature": 0.5},
    }


@respx.mock
async def test_chat_omits_empty_options_and_defaults_usage(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "hi"}})
    )

    result = await backend.chat("m", [], {})

    assert "options" not in json.loads(route.calls[0].request.content)
    assert result.usage.prompt_tokens == 0
    assert result.usage.completion_tokens == 0
    assert result.finish_reason == "stop"


@respx.mock
async def test_chat_stream_yields_deltas_then_final_usage(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(
            200,
            text=ndjson(
                {"message": {"content": "he"}, "done": False},
                {"message": {"content": "llo"}, "done": False},
                {
                    "message": {"content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 4,
                    "eval_count": 2,
                },
            )
            + "\n",
        )
    )

    deltas = await collect(backend.chat_stream("m", [], {}))

    assert [d.content for d in deltas] == ["he", "llo", ""]
    assert [d.done for d in deltas] == [False, False, True]
    assert deltas[0].usage is None
    assert deltas[-1].usage is not None
    assert deltas[-1].usage.prompt_tokens == 4
    assert deltas[-1].usage.completion_tokens == 2
    assert deltas[-1].finish_reason == "stop"


@respx.mock
async def test_chat_stream_error_line_raises(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(200, text=ndjson({"error": "model not loaded"}))
    )

    with pytest.raises(BackendUnavailable, match="model not loaded"):
        await collect(backend.chat_stream("m", [], {}))


@respx.mock
async def test_generate_uses_response_field(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/generate").mock(
        return_value=httpx.Response(200, json={"response": "text", "eval_count": 5})
    )

    result = await backend.generate("m", "once upon", {})

    assert result.content == "text"
    assert result.usage.completion_tokens == 5
    assert json.loads(route.calls[0].request.content)["prompt"] == "once upon"


@respx.mock
async def test_generate_stream_yields_deltas(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/generate").mock(
        return_value=httpx.Response(
            200,
            text=ndjson(
                {"response": "a", "done": False},
                {"response": "", "done": True, "prompt_eval_count": 1, "eval_count": 1},
            ),
        )
    )

    deltas = await collect(backend.generate_stream("m", "p", {}))

    assert [d.content for d in deltas] == ["a", ""]
    assert deltas[-1].finish_reason == "stop"


@respx.mock
async def test_embed_maps_embeddings_and_prompt_tokens(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 9})
    )

    result = await backend.embed("e", ["hello"])

    assert result.embeddings == [[0.1, 0.2]]
    assert result.usage.prompt_tokens == 9
    assert result.usage.completion_tokens == 0
    assert json.loads(route.calls[0].request.content) == {"model": "e", "input": ["hello"]}


@respx.mock
async def test_upstream_500_raises_backend_unavailable(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(BackendUnavailable, match="500"):
        await backend.chat("m", [], {})


@respx.mock
async def test_stream_upstream_500_raises_backend_unavailable(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(BackendUnavailable, match="500"):
        await collect(backend.chat_stream("m", [], {}))


@respx.mock
async def test_error_body_is_included_in_the_message(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(404, json={"error": "model 'm' not found"})
    )

    with pytest.raises(BackendUnavailable, match="Backend returned HTTP 404: model 'm' not found"):
        await backend.chat("m", [], {})


@respx.mock
async def test_stream_error_body_is_included_in_the_message(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(404, json={"error": "model 'm' not found"})
    )

    with pytest.raises(BackendUnavailable, match="404: model"):
        await collect(backend.chat_stream("m", [], {}))


@respx.mock
async def test_connect_error_twice_then_success(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/chat").mock(
        side_effect=[
            httpx.ConnectError("nope"),
            httpx.ConnectError("nope"),
            httpx.Response(200, json={"message": {"content": "ok"}}),
        ]
    )

    result = await backend.chat("m", [], {})

    assert result.content == "ok"
    assert route.call_count == 3


@respx.mock
async def test_connect_error_exhausts_retries(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/chat").mock(side_effect=httpx.ConnectError("nope"))

    with pytest.raises(BackendUnavailable, match="Cannot reach backend"):
        await backend.chat("m", [], {})

    assert route.call_count == 3


@respx.mock
async def test_read_timeout_raises_backend_timeout_without_retry(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/chat").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(BackendTimeout):
        await backend.chat("m", [], {})

    assert route.call_count == 1


@respx.mock
async def test_stream_connect_error_retries_then_succeeds(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/chat").mock(
        side_effect=[
            httpx.ConnectError("nope"),
            httpx.Response(200, text=ndjson({"message": {"content": "ok"}, "done": True})),
        ]
    )

    deltas = await collect(backend.chat_stream("m", [], {}))

    assert [d.content for d in deltas] == ["ok"]
    assert route.call_count == 2


@respx.mock
async def test_remote_protocol_error_maps_to_backend_unavailable(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(
        side_effect=httpx.RemoteProtocolError("server disconnected")
    )

    with pytest.raises(BackendUnavailable):
        await backend.chat("m", [], {})


@respx.mock
async def test_malformed_json_body_maps_to_backend_unavailable(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(return_value=httpx.Response(200, text="not json"))

    with pytest.raises(BackendUnavailable, match="malformed JSON"):
        await backend.chat("m", [], {})


@respx.mock
async def test_stream_remote_protocol_error_maps_to_backend_unavailable(
    backend: OllamaBackend,
) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(
            200,
            stream=_FailingStream(
                [b'{"message": {"content": "a"}, "done": false}\n'],
                httpx.RemoteProtocolError("server disconnected"),
            ),
        )
    )

    with pytest.raises(BackendUnavailable):
        await collect(backend.chat_stream("m", [], {}))


@respx.mock
async def test_stream_malformed_line_maps_to_backend_unavailable(backend: OllamaBackend) -> None:
    respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(200, text="{not json}\n")
    )

    with pytest.raises(BackendUnavailable):
        await collect(backend.chat_stream("m", [], {}))


@respx.mock
async def test_stream_is_never_replayed_after_yielding(backend: OllamaBackend) -> None:
    route = respx.post(f"{BACKEND_URL}/api/chat").mock(
        return_value=httpx.Response(
            200,
            stream=_FailingStream(
                [b'{"message": {"content": "a"}, "done": false}\n'],
                httpx.ConnectError("dropped"),
            ),
        )
    )

    seen = []
    with pytest.raises(BackendUnavailable):
        async for delta in backend.chat_stream("m", [], {}):
            seen.append(delta.content)

    assert seen == ["a"]
    assert route.call_count == 1

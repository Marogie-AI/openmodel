import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, TypeVar

import httpx

from app.errors import BackendTimeout, BackendUnavailable
from app.schemas.backend import ChatDelta, ChatResult, EmbedResult, Usage

T = TypeVar("T")


def _body(payload: dict[str, Any], options: dict[str, float | int], stream: bool) -> dict[str, Any]:
    body = {**payload, "stream": stream}
    if options:
        body["options"] = options
    return body


def _usage(data: dict[str, Any]) -> Usage:
    return Usage(
        prompt_tokens=data.get("prompt_eval_count") or 0,
        completion_tokens=data.get("eval_count") or 0,
    )


def _finish_reason(data: dict[str, Any]) -> Literal["stop", "length"]:
    return "length" if data.get("done_reason") == "length" else "stop"


def _chat_content(data: dict[str, Any]) -> str:
    message: dict[str, Any] = data.get("message") or {}
    return str(message.get("content", ""))


def _generate_content(data: dict[str, Any]) -> str:
    return str(data.get("response", ""))


class OllamaBackend:
    """Talks to one Ollama server, retrying only failures to establish a connection."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._retries = retries

    async def chat(
        self, model: str, messages: list[dict[str, str]], options: dict[str, float | int]
    ) -> ChatResult:
        body = _body({"model": model, "messages": messages}, options, stream=False)
        data = await self._post("/api/chat", body)
        return ChatResult(
            content=_chat_content(data), usage=_usage(data), finish_reason=_finish_reason(data)
        )

    def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, float | int]
    ) -> AsyncIterator[ChatDelta]:
        body = _body({"model": model, "messages": messages}, options, stream=True)
        return self._stream("/api/chat", body, _chat_content)

    async def generate(
        self, model: str, prompt: str, options: dict[str, float | int]
    ) -> ChatResult:
        body = _body({"model": model, "prompt": prompt}, options, stream=False)
        data = await self._post("/api/generate", body)
        return ChatResult(
            content=_generate_content(data), usage=_usage(data), finish_reason=_finish_reason(data)
        )

    def generate_stream(
        self, model: str, prompt: str, options: dict[str, float | int]
    ) -> AsyncIterator[ChatDelta]:
        body = _body({"model": model, "prompt": prompt}, options, stream=True)
        return self._stream("/api/generate", body, _generate_content)

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        data = await self._post("/api/embed", {"model": model, "input": inputs})
        return EmbedResult(
            embeddings=data.get("embeddings", []),
            usage=Usage(prompt_tokens=data.get("prompt_eval_count") or 0, completion_tokens=0),
        )

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._send(lambda: self._client.post(self._base_url + path, json=body))
        self._check(response.status_code)
        try:
            data: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise BackendUnavailable(f"Backend sent malformed JSON from {path}.") from exc
        return data

    async def _stream(
        self, path: str, body: dict[str, Any], content_of: Callable[[dict[str, Any]], str]
    ) -> AsyncIterator[ChatDelta]:
        async with contextlib.AsyncExitStack() as stack:
            # Only establishing the stream is retried; once bytes flow we never replay it.
            response = await self._send(
                lambda: stack.enter_async_context(
                    self._client.stream("POST", self._base_url + path, json=body)
                )
            )
            self._check(response.status_code)
            try:
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data: dict[str, Any] = json.loads(line)
                    if "error" in data:
                        raise BackendUnavailable(f"Backend error: {data['error']}")
                    done = bool(data.get("done"))
                    yield ChatDelta(
                        content=content_of(data),
                        done=done,
                        usage=_usage(data) if done else None,
                        finish_reason=_finish_reason(data) if done else None,
                    )
            except httpx.ReadTimeout as exc:
                raise BackendTimeout(f"Backend timed out on {path}.") from exc
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                raise BackendUnavailable(f"Backend stream failed on {path}: {exc}") from exc

    async def _send(self, send: Callable[[], Awaitable[T]]) -> T:
        """Run `send`, retrying connection failures and mapping transport errors to ApiError."""
        attempt = 0
        while True:
            try:
                return await send()
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt >= self._retries:
                    raise BackendUnavailable(f"Cannot reach backend at {self._base_url}.") from exc
                await asyncio.sleep(0.1 * 2**attempt)
                attempt += 1
            except httpx.ReadTimeout as exc:
                raise BackendTimeout(f"Backend at {self._base_url} timed out.") from exc
            except httpx.HTTPError as exc:
                raise BackendUnavailable(f"Backend request failed: {exc}") from exc

    def _check(self, status_code: int) -> None:
        if status_code >= 400:
            raise BackendUnavailable(f"Backend returned HTTP {status_code}.")

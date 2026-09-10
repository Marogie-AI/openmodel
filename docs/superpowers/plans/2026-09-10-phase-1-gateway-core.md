# Phase 1 — Gateway Core: implementation plan

Spec: `docs/specs/2026-09-10-openmodel-design.md` (binding authority).
Working dir: `/Users/phillipsuwumarogie/Desktop/mapi`. API code in `api/`.

## Global Constraints

- Python 3.12 via `uv`. Run everything as `cd api && uv run <cmd>`. Never pip.
- `ruff check .`, `ruff format --check .`, `mypy --strict app` and `pytest` must pass before every commit.
- Commits: conventional (`feat`/`test`/`chore`/`docs`), one behavior each, stage explicit paths, ≤5 files. End every commit message with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01NWcqgykTd7jaqTnZafZLa6
  ```
- No new dependencies beyond the ones Task 1 installs. No ORM, no DB, no auth in this phase.
- Every non-trivial branch has a test. Tests use `httpx.AsyncClient` + `ASGITransport` against the app, `respx` for Ollama. No test hits a real network except those marked `@pytest.mark.e2e`.
- Model names are real Ollama names (`qwen2.5:0.5b`, `qwen2.5-coder:0.5b`, `nomic-embed-text`). No aliases.
- OpenAI error envelope for every 4xx/5xx the app raises:
  `{"error": {"message": str, "type": str, "code": str | null}}`.
- Config only via environment (pydantic-settings). Nothing hardcoded that the spec lists as config.
- Keep files focused: one responsibility each, per the layout in the spec.

## Task 1: Project scaffold, settings, logging, `/health`

Create the `api/` project.

1. `cd /Users/phillipsuwumarogie/Desktop/mapi && uv init --python 3.12 --no-workspace --name openmodel-api api`; delete generated `main.py`/`hello.py`.
2. `uv add fastapi "uvicorn[standard]" httpx pydantic-settings structlog prometheus-client pyyaml` and `uv add --dev pytest pytest-asyncio respx ruff mypy types-pyyaml openai`.
3. `pyproject.toml` config:
   - `[tool.ruff]` `line-length = 100`, `target-version = "py312"`; `[tool.ruff.lint]` `select = ["E","F","I","UP","B","SIM"]`.
   - `[tool.mypy]` `strict = true`, `python_version = "3.12"`, `plugins = ["pydantic.mypy"]`.
   - `[tool.pytest.ini_options]` `asyncio_mode = "auto"`, `markers = ["e2e: needs running compose stack"]`, `addopts = "-m 'not e2e'"`.
4. `app/__init__.py` empty.
5. `app/config.py`: `class Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_prefix="OPENMODEL_")` and fields:
   `models_file: Path = Path("models.yaml")`, `log_level: str = "INFO"`, `environment: str = "dev"`,
   `backend_connect_timeout_s: float = 2.0`, `backend_read_timeout_s: float = 120.0`, `backend_retries: int = 2`, `ready_cache_ttl_s: float = 5.0`.
   Module-level `settings = Settings()`.
6. `app/logging.py`: `configure_logging(level: str)` sets structlog JSON renderer (`structlog.processors.JSONRenderer`), ISO timestamps, level filtering, and routes stdlib logging through structlog. Also `RequestIdMiddleware` (pure ASGI or Starlette `BaseHTTPMiddleware`): reads `X-Request-ID` header or generates `uuid4().hex`, binds it with `structlog.contextvars.bind_contextvars(request_id=...)`, clears after, sets `X-Request-ID` on the response.
7. `app/routes/__init__.py` empty; `app/routes/health.py`: router with `GET /health` → `{"status": "ok"}`.
8. `app/main.py`: `create_app() -> FastAPI` that calls `configure_logging`, adds `RequestIdMiddleware`, includes health router; module-level `app = create_app()`.
9. `tests/__init__.py` empty; `tests/conftest.py` with async fixture `client` yielding `httpx.AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")`.
10. `tests/test_health.py`: `/health` returns 200 `{"status":"ok"}`; response carries `X-Request-ID`; a supplied `X-Request-ID: abc` is echoed back.

Commits: `chore: scaffold api project with uv, ruff, mypy`; `feat: settings, structured logging, request id, /health`.

## Task 2: Model registry, `/v1/models`, `/ready`

1. `app/router.py`:
   ```python
   Capability = Literal["chat", "completion", "embedding"]

   class ModelSpec(BaseModel):
       name: str
       backend_url: str            # e.g. http://localhost:11434
       capabilities: list[Capability]

   class Registry:
       def __init__(self, models: list[ModelSpec]) -> None
       @classmethod
       def from_yaml(cls, path: Path) -> "Registry"   # file shape: {"models": [ModelSpec...]}
       def list(self) -> list[ModelSpec]              # sorted by name
       def get(self, name: str) -> ModelSpec          # raises UnknownModel(name)
       def require(self, name: str, capability: Capability) -> ModelSpec  # raises UnknownModel or CapabilityMismatch(name, capability)
       def backend_urls(self) -> set[str]
   ```
   `UnknownModel` and `CapabilityMismatch` are exceptions defined in `app/errors.py` together with a base `class ApiError(Exception)` carrying `status_code: int`, `message: str`, `type: str`, `code: str | None`. `UnknownModel` → 404 type `invalid_request_error` code `model_not_found`. `CapabilityMismatch` → 400 type `invalid_request_error` code `model_capability`. Register one `ApiError` exception handler in `main.py` that returns the OpenAI envelope.
2. `api/models.yaml` (dev default, used by compose and local runs):
   ```yaml
   models:
     - name: qwen2.5:0.5b
       backend_url: http://localhost:11434
       capabilities: [chat, completion]
     - name: nomic-embed-text
       backend_url: http://localhost:11434
       capabilities: [embedding]
   ```
3. `main.py`: build `Registry.from_yaml(settings.models_file)` in `create_app` and store on `app.state.registry`. `create_app(registry: Registry | None = None)` accepts an injected registry for tests.
4. `app/routes/models.py`: `GET /v1/models` → `{"object": "list", "data": [{"id": name, "object": "model", "created": 0, "owned_by": "openmodel"}]}`.
5. `app/routes/health.py`: add `GET /ready`. Readiness = registry non-empty AND every `backend_url` answers `GET {url}/api/tags` with 200 within the connect timeout. Cache the result for `settings.ready_cache_ttl_s`. Ready → 200 `{"status": "ready"}`; not ready → 503 `{"status": "not_ready", "backends": {url: "ok" | "<error>"}}`. Use a shared `httpx.AsyncClient` created in the FastAPI lifespan and stored on `app.state.http`.
6. Tests: `tests/test_registry.py` (from_yaml, get unknown raises, require mismatch raises, sorted list); `tests/test_models_route.py`; `tests/test_ready.py` with respx mocking `/api/tags` 200 → 200, connection error → 503 with backend detail, and cache (second call within TTL does not re-request: assert respx call_count == 1).

Commits: `feat: model registry and error envelope`; `feat: /v1/models`; `feat: /ready with backend probe`.

## Task 3: Ollama backend client

`app/backends/__init__.py` empty; `app/backends/ollama.py`.

Types (`app/schemas/backend.py`):
```python
class Usage(BaseModel): prompt_tokens: int; completion_tokens: int
class ChatResult(BaseModel): content: str; usage: Usage; finish_reason: Literal["stop","length"]
class ChatDelta(BaseModel): content: str; done: bool; usage: Usage | None = None; finish_reason: Literal["stop","length"] | None = None
class EmbedResult(BaseModel): embeddings: list[list[float]]; usage: Usage  # completion_tokens = 0
```

`class OllamaBackend` constructed with `(client: httpx.AsyncClient, base_url: str, retries: int)`; timeouts come from the shared client configured in lifespan with `httpx.Timeout(connect=settings.backend_connect_timeout_s, read=settings.backend_read_timeout_s, write=10, pool=10)`.

Methods:
- `async chat(model, messages: list[dict[str,str]], options: dict[str, float|int] ) -> ChatResult` → POST `/api/chat` `{"model", "messages", "stream": false, "options"}`. Map `prompt_eval_count`→prompt_tokens (default 0), `eval_count`→completion_tokens, `done_reason == "length"` → `"length"` else `"stop"`.
- `async chat_stream(...) -> AsyncIterator[ChatDelta]` → same with `"stream": true`, parse NDJSON lines; yield `content=msg["message"]["content"]`, final line (`done: true`) yields usage + finish_reason.
- `async generate(model, prompt, options) -> ChatResult` and `generate_stream` → `/api/generate` (`response` field instead of `message.content`).
- `async embed(model, inputs: list[str]) -> EmbedResult` → POST `/api/embed` `{"model","input": inputs}`; `embeddings` field; usage prompt_tokens from `prompt_eval_count` (default 0).
- `options` mapping done by caller; backend passes dict through.

Errors (`app/errors.py`): `BackendUnavailable` → 502 type `server_error` code `backend_unavailable`; `BackendTimeout` → 504 type `server_error` code `backend_timeout`. Retry policy: retry only `httpx.ConnectError`/`httpx.ConnectTimeout` up to `retries` times with `0.1 * 2**attempt` sleep; `httpx.ReadTimeout` → `BackendTimeout` immediately; non-2xx → `BackendUnavailable` with the upstream status in the message. Never retry once a stream has started yielding.

Tests `tests/test_ollama_backend.py` with respx: chat maps usage and finish_reason; chat_stream yields deltas and final usage; generate; embed; 500 → BackendUnavailable; ConnectError twice then 200 → succeeds and call_count == 3; ConnectError 3 times → BackendUnavailable; ReadTimeout → BackendTimeout, no retry.

Commit: `feat: ollama backend client with streaming and retries`.

## Task 4: `POST /v1/chat/completions` sync and streaming

`app/schemas/chat.py`:
- `ChatMessage(role: Literal["system","user","assistant"], content: str)`
- `ChatRequest(model: str, messages: list[ChatMessage] (min_length=1), stream: bool = False, temperature: float | None = None, top_p: float | None = None, max_tokens: int | None = None)` with `model_config = ConfigDict(extra="ignore")`.
- Response models mirroring OpenAI: `ChatCompletion(id, object="chat.completion", created: int, model, choices: list[ChatChoice], usage: UsageOut)` where `ChatChoice(index=0, message: ChatMessage, finish_reason)`, `UsageOut(prompt_tokens, completion_tokens, total_tokens)` (total computed in a validator, it is a wire field not storage).
- Chunk: `ChatCompletionChunk(id, object="chat.completion.chunk", created, model, choices: [ChunkChoice(index=0, delta: {"role"?: "assistant", "content"?: str}, finish_reason: str|None)], usage: UsageOut | None = None)`.

`app/routes/chat.py`:
- `id = "chatcmpl-" + uuid4().hex[:24]`, `created = int(time.time())`.
- Options mapping: `temperature→temperature`, `top_p→top_p`, `max_tokens→num_predict`; omit None.
- Resolve model via `registry.require(model, "chat")`. Backend obtained from `app.state.backends[spec.backend_url]` (dict built in lifespan, one `OllamaBackend` per distinct backend_url).
- Sync path returns `ChatCompletion`.
- Stream path returns `StreamingResponse(media_type="text/event-stream")` with headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`. First chunk delta `{"role":"assistant","content":""}`; content chunks; final chunk with `finish_reason` and `usage`; then `data: [DONE]\n\n`. Each event is `f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"`. If `request.is_disconnected()` becomes true, stop iterating (closing the backend response cancels the upstream request).
- Backend errors during the sync path propagate to the `ApiError` handler. During the stream, after headers are sent, emit one `data: {"error": {...}}\n\n` event then `[DONE]` and stop.

Tests `tests/test_chat.py` (respx-mocked backend): sync happy path shape incl. `usage.total_tokens`; unknown model 404 envelope; embedding model → 400 `model_capability`; validation 422 on empty messages; stream yields role chunk, content chunks, final usage chunk, `[DONE]`; backend 500 on sync → 502 envelope; mapping of max_tokens→num_predict asserted on the captured request body.

Commits: `feat: /v1/chat/completions`; `feat: chat completions streaming`.

## Task 5: `POST /v1/completions` and `POST /v1/embeddings`

`app/schemas/completions.py`: `CompletionRequest(model, prompt: str | list[str], stream=False, temperature, top_p, max_tokens)`; list prompts of length 1 accepted, longer lists → 400 type `invalid_request_error` code `prompt_list_unsupported`. Response `Completion(id="cmpl-…", object="text_completion", created, model, choices=[{"index":0,"text","finish_reason"}], usage)`. Stream chunks same object with `text` deltas, then `[DONE]`.

`app/routes/completions.py`: capability `"completion"`, backend `generate`/`generate_stream`. Same SSE mechanics as Task 4 (extract the shared SSE encoder into `app/sse.py`: `def sse(data: str) -> bytes` and `DONE = b"data: [DONE]\n\n"`; refactor chat route to use it in the same commit).

`app/schemas/embeddings.py`: `EmbeddingRequest(model, input: str | list[str])`; response `{"object":"list","data":[{"object":"embedding","index":i,"embedding":[...]}],"model","usage":{"prompt_tokens","total_tokens"}}`.

`app/routes/embeddings.py`: capability `"embedding"`, normalizes `input` to list, calls `backend.embed`.

Tests `tests/test_completions.py`, `tests/test_embeddings.py`: happy sync, stream, list prompt rejection, string vs list input, capability mismatch.

Commits: `refactor: shared sse encoder`; `feat: /v1/completions`; `feat: /v1/embeddings`.

## Task 6: Prometheus metrics

`app/metrics.py` with module-level instruments:
- `http_requests_total` Counter labels `method, path, status` (path = route template, e.g. `/v1/chat/completions`, not raw URL; unmatched → `unmatched`).
- `http_request_duration_seconds` Histogram labels `method, path`.
- `llm_tokens_total` Counter labels `direction ("prompt"|"completion"), model`.
- `llm_inflight` Gauge label `model`.
- `llm_ttft_seconds` Histogram label `model` (time from backend call start to first content delta; sync calls observe total latency).
- `llm_requests_total` Counter labels `model, endpoint ("chat"|"completion"|"embedding"), status ("ok"|"error")`.

Pure-ASGI `MetricsMiddleware` records the first two. Routes record the llm_* ones via a small helper `async with track(model, endpoint)` context manager in `metrics.py` that handles inflight inc/dec and status counting, plus `record_usage(model, usage)` and `observe_ttft(model, seconds)`.

`GET /metrics` returns `generate_latest()` with `CONTENT_TYPE_LATEST`. Excluded from `http_*` metrics.

Tests `tests/test_metrics.py`: after one chat call, `/metrics` body contains `llm_tokens_total{direction="prompt",model="qwen2.5:0.5b"}` with the mocked count, `llm_inflight{...} 0.0`, and `http_requests_total{method="POST",path="/v1/chat/completions",status="200"}`; after a 404 model, `llm_requests_total{...status="error"}` is absent and http 404 counted.

Commit: `feat: prometheus metrics`.

## Task 7: Dockerfile, compose, e2e acceptance, runbook

1. `api/Dockerfile`: multi-stage. Builder `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, `uv sync --frozen --no-dev --no-install-project`, then copy `app/` and `models.yaml`. Runtime `python:3.12-slim-bookworm`, copy `.venv`, create user `app` uid 10001, `USER 10001`, `ENV PATH=/app/.venv/bin:$PATH`, `WORKDIR /app`, `EXPOSE 8000`, `CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`, `HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"`. `.dockerignore` excludes `.venv`, tests, caches.
2. `compose.yaml` at repo root: services `ollama` (image `ollama/ollama:latest`, volume `ollama:/root/.ollama`, port 11434), `ollama-pull` (same image, `depends_on: ollama`, `entrypoint: ["/bin/sh","-c"]`, command pulls `qwen2.5:0.5b` and `nomic-embed-text` against `OLLAMA_HOST=http://ollama:11434`, `restart: "no"`), `api` (build `./api`, port `8000:8000`, env `OPENMODEL_MODELS_FILE=/app/models.compose.yaml`, mounts `./api/models.compose.yaml:/app/models.compose.yaml:ro`, `depends_on: ollama`). `api/models.compose.yaml` = models.yaml with `backend_url: http://ollama:11434`.
3. `tests/test_e2e.py` marked `e2e`: uses `openai.OpenAI(base_url=os.environ.get("OPENMODEL_BASE_URL","http://localhost:8000/v1"), api_key="unused")`. Asserts: `client.models.list()` contains `qwen2.5:0.5b`; `chat.completions.create` non-stream returns non-empty content and `usage.total_tokens > 0`; stream yields ≥2 chunks and last has `finish_reason == "stop"`; `completions.create` returns text; `embeddings.create(model="nomic-embed-text", input="hello")` returns 768 floats. Run with `uv run pytest -m e2e`.
4. `docs/runbooks/01-gateway-core.md`: commands to run (compose up, curl sync/stream, `/metrics`, `/ready`), what to observe, `docker ps/logs/exec/stop` tour, image-vs-container note, break-it exercise: `docker compose stop ollama` → chat returns 502 envelope, `/ready` 503 with backend detail; `start` → recovers within 5s cache TTL.
5. Verify: `docker compose up -d --build`, wait for pull, `uv run pytest -m e2e` green. Paste the passing output into the report.

Commits: `chore: dockerfile and compose stack`; `test: openai sdk e2e acceptance`; `docs: runbook 01 gateway core`.

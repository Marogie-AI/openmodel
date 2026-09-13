# Runbook 01 — Gateway core

The OpenAI-compatible gateway in front of Ollama, running under Docker Compose.

From Phase 2 on, every `/v1` request needs `Authorization: Bearer <key>`, and
the curl commands below return 401 without one. Make a key first —
[runbook 02](02-platform-layer.md) has the three admin calls that mint one.

## Run it

From the repo root:

```sh
docker compose up -d --build
```

Three services start:

- `ollama` — the model server, weights in the named volume `mapi_ollama`.
- `ollama-pull` — a one-shot job that pulls `qwen2.5:0.5b` and `nomic-embed-text`, then exits 0. The first run downloads ~700 MB.
- `api` — the gateway on <http://localhost:8000>.

`api` only waits for `ollama` to start, not for the pull to finish. Readiness
means the backends answer, not that the models are pulled: on a cold volume
`/ready` is already 200 while the weights download, and a chat request fails
with `502 "Backend returned HTTP 404: model ... not found"` until they land.
Watch the download:

```sh
docker compose logs -f ollama-pull
```

When it prints `success` and exits, you are ready.

If host port 8000 is already taken, publish somewhere else:
`OPENMODEL_HOST_PORT=8010 docker compose up -d`.

## Observe

```sh
curl localhost:8000/health
curl localhost:8000/ready
curl localhost:8000/v1/models

curl localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Say hi."}]}'

curl -N localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Say hi."}],"stream":true}'

curl localhost:8000/metrics
```

The end-to-end acceptance tests run against the live stack (default port 8000):

```sh
cd api && OPENMODEL_BASE_URL=http://localhost:8010/v1 uv run pytest -m e2e
```

What to look for:

- `/health` answers immediately and never touches the backend — it says the
  process is alive, nothing more.
- `/ready` asks the backends and caches the answer for 5 seconds.
- The streaming call emits `data:` lines ending in `data: [DONE]`. The last
  chunk before that carries `finish_reason` and the `usage` totals together.
- `/metrics` exposes `http_requests_total`, `http_request_duration_seconds`,
  `llm_requests_total`, `llm_tokens_total`, `llm_ttft_seconds` and
  `llm_inflight`. Send a few requests, curl it again, watch the counters move.

## Docker tour

```sh
docker compose ps            # what is running, and its health
docker compose logs -f api   # structured JSON logs from the gateway
docker compose exec api sh   # a shell inside the running container
docker compose stop api      # stop without deleting
docker compose down          # stop and remove containers and network
docker compose down -v       # ...and delete the model volume (re-download)
```

Image vs container: the image is the built filesystem, produced once by
`docker compose build` from `api/Dockerfile`. A container is one running
instance of that image. Editing code on your Mac changes neither — rebuild
(`docker compose up -d --build`) to get it into the image. The exception is
`api/models.compose.yaml`, which is bind-mounted read-only into the container,
so editing it needs only a `docker compose restart api`.

## Break it

Stop the backend:

```sh
docker compose stop ollama
```

Then a chat request returns the OpenAI-shaped error envelope with HTTP 502:

```json
{"error":{"message":"Cannot reach backend at http://ollama:11434.","type":"server_error","code":"backend_unavailable"}}
```

and `/ready` returns 503 naming the backend that is down:

```json
{"status":"not_ready","backends":{"http://ollama:11434":"ConnectError: [Errno -2] Name or service not known"}}
```

Bring it back:

```sh
docker compose start ollama
```

Chat recovers on the next request. `/ready` can lag up to 5 seconds — that is
the readiness cache TTL, not a stuck service.

## What to notice

- Liveness and readiness are different questions. `/health` stays 200 through
  the whole outage; only `/ready` turns red.
- Backend failures come back as OpenAI-shaped errors, so an OpenAI SDK client
  raises a normal API error instead of choking on an unfamiliar body.
- The gateway holds no state. Kill and recreate the `api` container as often as
  you like; the weights live in the `ollama` volume.

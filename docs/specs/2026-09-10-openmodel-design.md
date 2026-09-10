# OpenModel API — Production-Shaped Build Plan

## Context

Empty dir `~/Desktop/mapi`. Build an OpenAI-compatible inference platform on
Kubernetes to learn the stack. Final architecture built directly, no
Pod-then-Deployment tutorial steps. I build; each phase ends with a runbook
(what to run, what to observe, what to break).

Machine: Apple Silicon, 16GB, Docker Desktop 8GB/6CPU (user raises to 12GB/8CPU),
gh logged in as pumarogie. Installed: ollama, docker, kubectl, uv. To install:
kind, helm, k6, cert-manager/envoy via manifests.

## Decisions (grilled, all settled)

| Area | Decision |
|---|---|
| Purpose | Private sandbox repo `pumarogie/openmodel` |
| Cluster | kind, Calico CNI, Envoy Gateway (Gateway API), cert-manager self-signed TLS, host `api.openmodel.test` via /etc/hosts, ports 80/443 mapped |
| Manifests | Kustomize `base/` + `overlays/{dev,ci}`. No authored Helm chart. Helm only consumes third-party charts |
| Namespaces | `openmodel-edge`, `openmodel-api`, `openmodel-inference`, `openmodel-data`, `openmodel-monitoring`. PodSecurity `restricted` (data: `baseline`) |
| Tenancy | organizations → users → api_keys. Admin creates org, user, first key via `X-Admin-Token` endpoints. Users self-manage keys with any own key |
| Plans | `plans` table (rpm, concurrency) + `plan_models` junction. Org has `plan_code` FK. Seeded by migration, `/admin/plans` edits. See schema section |
| Keys | `om_<env>_<key_id:12>_<secret:32>`. Store argon2id(secret+pepper), pepper in Secret. Redis verify cache 300s, dropped on revoke. Soft delete `revoked_at` |
| Rate limit | Per org: Redis Lua token bucket (rpm) + concurrency cap (INCR/DECR). 429 with `Retry-After`, `X-RateLimit-*`. No TPM |
| OpenAI compat | `/v1/models`, `/v1/chat/completions` (sync+SSE), `/v1/completions` (sync+SSE, via Ollama `/api/generate`), `/v1/embeddings`. Acceptance test: official `openai` Python SDK with `base_url` |
| Models | Real names exposed: `qwen2.5:0.5b`, `qwen2.5-coder:0.5b`, `nomic-embed-text`. Registry ConfigMap: name → backend URL, capabilities, plans |
| Inference | One Ollama Deployment+Service+PVC per chat model. Embedding model lives in small-model pod. Weights via one-shot pull Job per PVC |
| Data | Postgres 18 (`postgres:18` image; note PG18 data dir moved to `/var/lib/postgresql/18/docker`, mount `/var/lib/postgresql`) + Redis 7 as StatefulSet, 1 replica, volumeClaimTemplate |
| DB layer | SQLAlchemy 2 async + Alembic. Migrations: Job per deploy; api initContainer polls `alembic current == head` |
| Usage | `requests` row per call via background task. `GET /api/usage?from&to&group_by=model\|user\|day`, org-wide for any org key. No retention policy |
| Dev loop | docker compose (postgres, redis, ollama, api). Tests hit real Postgres/Redis; DB tests skip if unreachable |
| Observability | structlog JSON + request_id. Prometheus via kube-prometheus-stack, ServiceMonitor, Grafana dashboard ConfigMap, alert rules (no receiver) |
| Scaling | HPA 2..10 on custom `llm_inflight` via prometheus-adapter. Model pods manual. k6 load tests |
| Registry | Private GHCR, `imagePullSecret` in api namespace. Local dev uses `kind load` |
| CI | GitHub Actions: ruff, mypy strict, pytest with service containers, multi-arch build (arm64+amd64), push GHCR, kind-in-CI e2e with real qwen2.5:0.5b pull |
| Python | 3.12 via uv, pydantic-settings, httpx, structlog, prometheus-client, argon2-cffi |

## Target architecture

```
client ──HTTPS api.openmodel.test──▶ Envoy Gateway (openmodel-edge, cert-manager TLS)
                                        │ HTTPRoute /v1/*, /api/*, /admin/*
                                        ▼
                             api Deployment (openmodel-api, HPA 2..10)
                             auth → ratelimit → router → backend → usage
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              ▼                         ▼                          ▼
   postgres StatefulSet        redis StatefulSet        ollama-qwen / ollama-coder
      (openmodel-data)           (openmodel-data)           (openmodel-inference)
 openmodel-monitoring: kube-prometheus-stack, prometheus-adapter, Grafana
 NetworkPolicy: default-deny each namespace, explicit allows only
```

## Repo layout

```
mapi/
├── api/
│   ├── app/
│   │   ├── main.py, config.py, logging.py, metrics.py
│   │   ├── routes/{health,models,chat,completions,embeddings,keys,usage,admin}.py
│   │   ├── auth/{keys.py,deps.py}
│   │   ├── ratelimit.py, router.py, usage.py
│   │   ├── backends/ollama.py
│   │   ├── db/{models.py,session.py}
│   │   └── schemas/*.py
│   ├── alembic/, tests/, pyproject.toml, Dockerfile
├── compose.yaml
├── kubernetes/
│   ├── kind/ (cluster.yaml, bootstrap.sh: calico, envoy-gateway, cert-manager, hosts line)
│   ├── base/ (namespaces, api, inference, data, edge, policies, kustomization)
│   └── overlays/{dev,ci}/
├── monitoring/ (helm values, dashboard json, adapter rules, alert rules)
├── scripts/ (secrets.sh, load-test/k6.js, chaos/*.sh)
├── .github/workflows/ci.yaml
└── docs/ (specs/, runbooks/, adr/)
```

## Phases

| # | Phase | Delivers |
|---|---|---|
| 1 | Gateway core | FastAPI: models, chat, completions, embeddings (sync+SSE), registry, Ollama backend, metrics, JSON logs, Dockerfile, compose, openai-SDK acceptance test, ruff+mypy |
| 2 | Platform layer | Alembic schema (orgs, users, api_keys, requests), argon2 keys + cache, admin endpoints, key CRUD, rate limit + concurrency, usage endpoint |
| 3 | Cluster | kind bootstrap (Calico, Envoy Gateway, cert-manager), namespaces + PSA, Kustomize base/overlays, StatefulSets, PVC + pull Jobs, migrate Job + initContainer, probes, resources, Secrets, imagePullSecret, NetworkPolicies, Gateway + HTTPRoute + TLS |
| 4 | Ops + scale | kube-prometheus-stack, ServiceMonitor, dashboard, alerts, prometheus-adapter, HPA on llm_inflight, k6, second model routing, bottleneck runbook |
| 5 | Delivery + chaos | GitHub Actions (lint, type, tests, multi-arch build, GHCR, kind e2e), chaos scripts + runbook, ADRs |

Each phase: spec in `docs/specs/`, runbook in `docs/runbooks/`, verified end to
end before next phase planned in detail.

---

## Phase 1 detail — Gateway core

Commits, in order:

1. `chore`: git init, `.gitignore`, README (architecture, decisions table, phases).
2. `chore`: `uv init api` (py3.12), deps, ruff + mypy strict config, `config.py`
   Settings, `logging.py` structlog JSON + request_id middleware.
3. `feat`: `/health`, `/ready` (registry loaded + each backend reachable, cached 5s).
4. `feat`: `router.py` registry from `MODELS_FILE` yaml (name, backend_url,
   capabilities[chat|completion|embedding]); `/v1/models`.
5. `feat`: `backends/ollama.py`: `chat`, `generate`, `embed`, streaming variants,
   usage mapping, timeouts (connect 2s, read 120s), retry connect errors only. respx tests.
6. `feat`: `POST /v1/chat/completions` sync. OpenAI shape + error envelope
   (404 unknown model, 400 capability mismatch, 502 backend down, 504 timeout).
7. `feat`: chat streaming SSE `chat.completion.chunk`, `[DONE]`, disconnect cancels.
8. `feat`: `POST /v1/completions` sync + stream.
9. `feat`: `POST /v1/embeddings` (string or list input).
10. `feat`: `metrics.py`: `http_requests_total`, `http_request_duration_seconds`,
    `llm_tokens_total{direction,model}`, `llm_inflight{model}`, `llm_ttft_seconds`. `/metrics`.
11. `test`: acceptance test with `openai` SDK against running compose stack (marked `e2e`).
12. `chore`: Dockerfile multi-stage, non-root, ro rootfs friendly; `compose.yaml`
    with ollama volume + one-shot pull service.
13. `docs`: runbook 01.

Verification:

```
cd api && uv run ruff check . && uv run mypy app && uv run pytest
docker compose up -d && uv run pytest -m e2e
curl -N localhost:8000/v1/chat/completions -d '{"model":"qwen2.5:0.5b","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

Done when: lint/type/tests green, openai SDK sync + stream + embeddings pass
against compose, `/ready` flips to 503 when Ollama stopped.

## Database schema (3NF)

Every non-key column depends on the key, the whole key, and nothing but the key.
No derived or transitively-derivable columns stored.

Table names singular. Database name `openmodel`.

```
plan
  code               text PK            -- 'free', 'pro'
  requests_per_minute int NOT NULL
  max_concurrency     int NOT NULL

plan_model                               -- which models a plan may call
  plan_code   text FK→plan.code
  model_name  text                       -- matches registry name
  PK (plan_code, model_name)

organization
  id          uuid PK
  name        text UNIQUE NOT NULL
  plan_code   text FK→plan.code NOT NULL
  created_at  timestamptz NOT NULL

app_user                                 -- 'user' is reserved in Postgres
  id               uuid PK
  organization_id  uuid FK→organization.id NOT NULL
  email            text UNIQUE NOT NULL
  created_at       timestamptz NOT NULL

api_key
  id           uuid PK
  user_id      uuid FK→app_user.id NOT NULL   -- org derived via user, not stored
  key_id       text UNIQUE NOT NULL        -- public lookup part
  secret_hash  text NOT NULL               -- argon2id(secret + pepper)
  prefix       text NOT NULL               -- display 'om_live_abc…'
  created_at   timestamptz NOT NULL
  revoked_at   timestamptz NULL

request
  id                 bigint PK (identity)
  api_key_id         uuid FK→api_key.id NOT NULL  -- user/org derived, not stored
  model_name         text NOT NULL
  endpoint           text NOT NULL        -- 'chat' | 'completion' | 'embedding'
  prompt_tokens      int NOT NULL
  completion_tokens  int NOT NULL         -- total_tokens derived, not stored
  latency_ms         int NOT NULL
  status_code        smallint NOT NULL
  created_at         timestamptz NOT NULL
  INDEX (api_key_id, created_at)
```

Consequences:
- Plan limits move from ConfigMap into `plans` table (supersedes Q3). Seeded by
  migration, editable via `/admin/plans`. Registry ConfigMap keeps only backend routing.
- Usage aggregation joins request → api_key → app_user → organization. Fine at
  this scale; ADR notes a materialized daily rollup as the upgrade path.
- Rate limiter reads plan limits through the cached principal (org + plan) so no
  DB hit per request.

## Postgres operations (from ops review of this design)

Applies to compose and cluster alike. Config shipped as ConfigMap mounted and
passed via `-c config_file=`; roles/init via `docker-entrypoint-initdb.d` SQL.

**Roles.** Image superuser `postgres` used only by entrypoint. Init SQL creates
`openmodel_owner` (owns db, runs Alembic, migrate Job) and `openmodel_app`
(DML on `public` only, API runtime). Two Secrets, two DSNs. Superuser password
never handed to app pods.

**Config (`postgresql.conf` ConfigMap, pod limit 1Gi):**
```
shared_buffers = 256MB          effective_cache_size = 768MB
work_mem = 8MB                  maintenance_work_mem = 64MB
random_page_cost = 1.1          max_connections = 50
shared_preload_libraries = 'pg_stat_statements'
track_io_timing = on
log_min_duration_statement = 250ms   log_lock_waits = on
log_temp_files = 0                   log_checkpoints = on
log_line_prefix = '%m [%p] %q%u@%d app=%a '
```
Logs stay on stderr (container norm), `logging_collector` off.

**Timeouts on roles, not global:**
```sql
ALTER ROLE openmodel_app SET statement_timeout = '10s';
ALTER ROLE openmodel_app SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE openmodel_app SET lock_timeout = '2s';
ALTER ROLE openmodel_owner SET lock_timeout = '5s';   -- DDL fails, never queues behind readers
```

**Connection budget.** SQLAlchemy async pool per api pod `pool_size=3,
max_overflow=2`; HPA max 10 pods → 50 = `max_connections`. Migrate Job + humans
use the remaining headroom only when pods are fewer. ADR: pgBouncer transaction
mode when pool math breaks.

**Indexes.** Drop the standalone `request(created_at)` index from the schema;
`(api_key_id, created_at)` covers org-scoped usage queries. Unique indexes on
`api_key.key_id`, `app_user.email`, `organization.name` do double duty as lookups.
Phase 4 runbook checks `pg_stat_user_indexes.idx_scan` after k6 runs.

**Access.** `listen_addresses='*'` inside pod is fine; NetworkPolicy allows only
api and migrate Job. Image default `pg_hba`: scram for host, trust on local
socket only. In-cluster TLS off; ADR notes cert-manager-issued server cert as
upgrade path.

**Monitoring (Phase 4).** `postgres-exporter` sidecar + ServiceMonitor. Alerts:
PVC usage > 80% (`kubelet_volume_stats_used_bytes`), `pg_up == 0`,
oldest `backend_xmin` age, dead tuple ratio. Grafana panel for
`pg_stat_statements` top queries.

**Backups (Phase 5).** CronJob nightly `pg_dump -Fc` to a backup PVC, 7-day
retention. Chaos runbook includes a timed restore into a fresh StatefulSet,
recording RTO. No PITR: `archive_mode` stays off, documented as out of scope
for kind; pgBackRest named in ADR.

**Autovacuum.** Left on, defaults. `request` is append-only so dead tuples come
only from `api_key` revokes; nothing hot. Wraparound irrelevant at this volume;
runbook still shows `age(datfrozenxid)` query.

**Version.** `postgres:18` tag (floats minor, receives fixes). PG18 data dir is
`/var/lib/postgresql/18/docker`; volumeClaimTemplate mounts `/var/lib/postgresql`.
PG18 async IO `io_method` left at default `worker`.

## Phase 2 outline — Platform layer

- Alembic init with schema above; seed migration inserts `free`, `pro` plans + plan_models.
- `auth/keys.py` generate/verify (argon2id + pepper); `auth/deps.py` Bearer → principal
  (user+org), Redis cache; 401 envelope.
- `/admin/orgs`, `/admin/users`, `/admin/users/{id}/keys` behind `X-Admin-Token`.
- `/api/keys` CRUD scoped to caller's user; DELETE = revoke + cache drop.
- `ratelimit.py` Lua token bucket + concurrency cap per org from plan config; 429 headers.
- `usage.py` background writer; `/api/usage` SQL aggregation with group_by.
- Plan gating: model allowed per plan → 403 otherwise.
- Tests against compose postgres/redis; openai SDK acceptance now with real key.

Phases 3-5 detailed when Phase 2 ships.

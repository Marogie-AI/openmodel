# Phase 2 — Platform Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 1 gateway into a multi-tenant platform: Postgres-backed orgs/users/keys, argon2 API-key auth with Redis cache, per-org rate limiting and concurrency caps, plan-based model gating, and usage metering with an aggregation endpoint.

**Architecture:** SQLAlchemy 2 async + Alembic over Postgres 18 (3NF, singular tables). Auth is a FastAPI dependency resolving `Bearer om_<env>_<key_id>_<secret>` to a cached `Principal`. Rate limiting is an atomic Redis Lua token bucket plus INCR/DECR concurrency cap keyed per org. Usage rows are written by a background task after each LLM call. Admin surface behind `X-Admin-Token`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (asyncio) + asyncpg, Alembic, redis-py (asyncio), argon2-cffi, Postgres 18, Redis 7, docker compose for dev/tests.

**Spec:** `docs/specs/2026-09-10-openmodel-design.md` (sections: Decisions table, Database schema (3NF), Postgres operations, Phase 2 outline). Phase 1 code is on `master`.

## Global Constraints

- Working dir `/Users/phillipsuwumarogie/Desktop/mapi`; code in `api/`; run everything as `cd api && uv run <cmd>`. Never pip.
- `ruff check .`, `ruff format --check .`, `mypy --strict app`, `pytest` green before every commit. Pristine test output.
- New runtime deps allowed this phase, exactly: `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `redis`, `argon2-cffi`. Dev: `pytest-env` is NOT allowed; use env defaults. Nothing else.
- Conventional commits, one behavior each, explicit staged paths, ≤5 files where possible; end each message with a Co-Authored-By trailer for the implementing model plus `Claude-Session: https://claude.ai/code/session_01NWcqgykTd7jaqTnZafZLa6`.
- Schema is 3NF with singular table names: `plan`, `plan_model`, `organization`, `app_user`, `api_key`, `request`. No derived columns (no `total_tokens`, no `organization_id` on `api_key` or `request`).
- Key format `om_<env>_<key_id>_<secret>`: `env` = `settings.environment` (`dev`/`live`), `key_id` = 12 chars `secrets.token_urlsafe(9)`, `secret` = 32 chars `secrets.token_urlsafe(24)`. Store `argon2id(secret + pepper)`. Raw key returned once at creation. Never log a raw key or secret.
- All app-raised 4xx/5xx use the OpenAI envelope via `ApiError` subclasses in `app/errors.py`.
- Config only via `Settings` (`OPENMODEL_` env prefix). Two DSNs: `database_url` (app role) and `database_owner_url` (owner role, migrations only).
- Tests needing Postgres/Redis are marked `@pytest.mark.db` and skip cleanly when the services are unreachable (`compose up` provides them). Every db test starts from truncated tables. Unit tests that do not need the DB keep working without compose.
- Routes under `/v1/*` and `/api/*` require a valid key; `/health`, `/ready`, `/metrics`, `/admin/*` (admin token) do not.

## File map

```
api/app/
  config.py                 # + database_url, database_owner_url, redis_url, key_pepper, admin_token, auth_cache_ttl_s
  db/__init__.py
  db/models.py              # SQLAlchemy 2 declarative models, 6 tables
  db/session.py             # engine + async_sessionmaker factory; get_session dependency
  auth/__init__.py
  auth/keys.py              # generate_key(), hash_secret(), verify_secret(), parse_key()
  auth/principal.py         # Principal dataclass; resolve_principal(); Redis cache; require_principal dependency
  auth/admin.py             # require_admin dependency (X-Admin-Token)
  ratelimit.py              # TokenBucket Lua, ConcurrencyCap, enforce(principal) dependency
  usage.py                  # record(...) background writer; aggregate(...) query
  routes/admin.py           # /admin/plans, /admin/orgs, /admin/users, /admin/users/{id}/keys
  routes/keys.py            # /api/keys CRUD
  routes/usage.py           # /api/usage
  schemas/admin.py, schemas/keys.py, schemas/usage.py
api/alembic.ini, api/alembic/env.py, api/alembic/versions/0001_initial.py, 0002_seed_plans.py
api/sql/postgresql.conf      # spec "Postgres operations" config
api/sql/init/01_roles.sql    # owner/app roles + timeouts (docker-entrypoint-initdb.d)
compose.yaml                 # + postgres, redis, migrate; api env + depends_on
api/tests/conftest.py        # + db/redis fixtures, skip logic, truncate, admin/key helpers
docs/runbooks/02-platform-layer.md
```

---

### Task 1: Data services, dependencies, settings, session factory

**Files:**
- Modify: `api/pyproject.toml` (deps), `api/app/config.py`, `compose.yaml`, `api/tests/conftest.py`
- Create: `api/sql/postgresql.conf`, `api/sql/init/01_roles.sql`, `api/app/db/__init__.py`, `api/app/db/session.py`, `api/tests/test_db_session.py`

**Interfaces:**
- Produces: `settings.database_url: str` (default `postgresql+asyncpg://openmodel_app:app@localhost:5432/openmodel`), `settings.database_owner_url: str` (default `postgresql+asyncpg://openmodel_owner:owner@localhost:5432/openmodel`), `settings.redis_url: str` (default `redis://localhost:6379/0`), `settings.key_pepper: str` (default `dev-pepper-change-me`), `settings.admin_token: str` (default `dev-admin-token`), `settings.auth_cache_ttl_s: int = 300`, `settings.db_pool_size: int = 3`, `settings.db_max_overflow: int = 2`.
- Produces: `app/db/session.py`: `make_engine(url: str) -> AsyncEngine` (pool_size/max_overflow from settings, `pool_pre_ping=True`), `make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]`, and `async def get_session(request: Request) -> AsyncIterator[AsyncSession]` FastAPI dependency yielding from `request.app.state.sessionmaker` (commit on success, rollback on exception).
- Produces in lifespan (`app/main.py`): `app.state.engine`, `app.state.sessionmaker`, `app.state.redis: redis.asyncio.Redis` (from `redis.asyncio.from_url(settings.redis_url, decode_responses=True)`), all disposed/closed on shutdown.
- Produces test fixtures in `conftest.py`: `db_available()` / `redis_available()` helpers (try connect with 1s timeout); `pytestmark`-style `db` marker registered in pyproject (`markers = ["e2e: ...", "db: needs postgres and redis from compose"]`) with an autouse-per-marker fixture that skips when unavailable; `session` fixture (owner engine, yields a session, truncates all six tables `TRUNCATE plan_model, request, api_key, app_user, organization, plan RESTART IDENTITY CASCADE` before each test then re-seeds plans by calling the seed function from Task 2 — until Task 2 lands, only truncate); `redis_client` fixture that `FLUSHDB`s before each test; `client` fixture unchanged for non-db tests.

- [ ] Add deps: `uv add "sqlalchemy[asyncio]" asyncpg alembic redis argon2-cffi`.
- [ ] `api/sql/postgresql.conf` exactly per spec "Postgres operations → Config": shared_buffers 256MB, effective_cache_size 768MB, work_mem 8MB, maintenance_work_mem 64MB, random_page_cost 1.1, max_connections 50, `shared_preload_libraries = 'pg_stat_statements'`, track_io_timing on, log_min_duration_statement 250ms, log_lock_waits on, log_temp_files 0, log_checkpoints on, `log_line_prefix = '%m [%p] %q%u@%d app=%a '`, `listen_addresses = '*'`.
- [ ] `api/sql/init/01_roles.sql` (runs once as superuser on first boot, db `openmodel` created by `POSTGRES_DB`):
  ```sql
  CREATE ROLE openmodel_owner LOGIN PASSWORD 'owner';
  CREATE ROLE openmodel_app LOGIN PASSWORD 'app';
  ALTER DATABASE openmodel OWNER TO openmodel_owner;
  GRANT CONNECT ON DATABASE openmodel TO openmodel_app;
  \c openmodel
  ALTER SCHEMA public OWNER TO openmodel_owner;
  GRANT USAGE ON SCHEMA public TO openmodel_app;
  ALTER DEFAULT PRIVILEGES FOR ROLE openmodel_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openmodel_app;
  ALTER DEFAULT PRIVILEGES FOR ROLE openmodel_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO openmodel_app;
  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
  ALTER ROLE openmodel_app SET statement_timeout = '10s';
  ALTER ROLE openmodel_app SET idle_in_transaction_session_timeout = '30s';
  ALTER ROLE openmodel_app SET lock_timeout = '2s';
  ALTER ROLE openmodel_owner SET lock_timeout = '5s';
  ```
- [ ] `compose.yaml`: add
  ```yaml
  postgres:
    image: postgres:18
    environment: {POSTGRES_PASSWORD: postgres, POSTGRES_DB: openmodel}
    command: ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
    ports: ["5432:5432"]
    volumes:
      - postgres:/var/lib/postgresql
      - ./api/sql/postgresql.conf:/etc/postgresql/postgresql.conf:ro
      - ./api/sql/init:/docker-entrypoint-initdb.d:ro
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres -d openmodel"], interval: 2s, timeout: 2s, retries: 30}
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck: {test: ["CMD", "redis-cli", "ping"], interval: 2s, timeout: 2s, retries: 30}
  ```
  `api` gets `depends_on: {postgres: {condition: service_healthy}, redis: {condition: service_healthy}, ollama: {condition: service_started}}` and env `OPENMODEL_DATABASE_URL=postgresql+asyncpg://openmodel_app:app@postgres:5432/openmodel`, `OPENMODEL_DATABASE_OWNER_URL=postgresql+asyncpg://openmodel_owner:owner@postgres:5432/openmodel`, `OPENMODEL_REDIS_URL=redis://redis:6379/0`. Add `postgres` to `volumes:`. (Migrate service added in Task 2.)
- [ ] `config.py` fields as listed in Interfaces. `session.py` as specified. `main.py` lifespan creates engine (app url), sessionmaker, redis; closes both on exit. Unit tests that don't touch the DB must still pass without compose: the engine is created lazily by SQLAlchemy (no connection at construction), and redis `from_url` does not connect until used — verify this holds.
- [ ] `tests/conftest.py` fixtures per Interfaces. `tests/test_db_session.py` (marked `db`): `get_session` yields a session that can `SELECT 1`; a dependency that raises rolls back (insert into a temp table inside, raise, assert not present — until tables exist use `SELECT` only and assert the session is closed after the request).
- [ ] Verify: `docker compose up -d postgres redis` → both healthy; `uv run pytest` green (db tests run); `docker compose stop postgres redis` → db tests skip, rest green.
- [ ] Commits: `chore: add postgres and redis to compose with roles and config`; `feat: database and redis settings, session factory, lifespan wiring`; `test: db fixtures with availability skip`.

---

### Task 2: Schema models, Alembic migrations, seed

**Files:**
- Create: `api/app/db/models.py`, `api/alembic.ini`, `api/alembic/env.py`, `api/alembic/script.py.mako`, `api/alembic/versions/0001_initial.py`, `api/alembic/versions/0002_seed_plans.py`, `api/tests/test_migrations.py`
- Modify: `compose.yaml` (migrate service), `api/tests/conftest.py` (seed after truncate), `api/Dockerfile` (copy `alembic/` and `alembic.ini`)

**Interfaces:**
- Produces `app/db/models.py` (`class Base(DeclarativeBase)`; all timestamps `timestamptz` via `DateTime(timezone=True)`, `server_default=func.now()`; uuid PKs `Uuid(as_uuid=True)` with `default=uuid4`):
  - `Plan(__tablename__="plan")`: `code: str PK`, `requests_per_minute: int`, `max_concurrency: int`.
  - `PlanModel("plan_model")`: `plan_code: str FK plan.code ON DELETE CASCADE`, `model_name: str`; composite PK.
  - `Organization("organization")`: `id: UUID`, `name: str unique`, `plan_code: str FK plan.code`, `created_at`.
  - `AppUser("app_user")`: `id: UUID`, `organization_id: UUID FK organization.id`, `email: str unique`, `created_at`.
  - `ApiKey("api_key")`: `id: UUID`, `user_id: UUID FK app_user.id`, `key_id: str unique`, `secret_hash: str`, `prefix: str`, `created_at`, `revoked_at: datetime | None`.
  - `Request("request")`: `id: int PK Identity`, `api_key_id: UUID FK api_key.id`, `model_name: str`, `endpoint: str`, `prompt_tokens: int`, `completion_tokens: int`, `latency_ms: int`, `status_code: int (SmallInteger)`, `created_at`; `Index("ix_request_api_key_id_created_at", "api_key_id", "created_at")`.
  - Relationships: `AppUser.organization`, `ApiKey.user`, `Organization.plan` (lazy="selectin" only where the auth path needs them: `ApiKey.user` → `AppUser.organization` → `Organization.plan`).
- Produces `app/db/seed.py`: `async def seed_plans(session) -> None` idempotent upsert of plans `free (60 rpm, 2 concurrency)` and `pro (600 rpm, 10 concurrency)` and plan_model rows: free → `qwen2.5:0.5b`, `nomic-embed-text`; pro → those plus `qwen2.5-coder:0.5b`. Migration 0002 calls the same data via `op.bulk_insert` (keep one source of truth: define `PLAN_ROWS` and `PLAN_MODEL_ROWS` constants in `seed.py`, imported by both).
- Alembic: `env.py` async, url from `settings.database_owner_url` (override via `-x url=`), `target_metadata = Base.metadata`, `compare_type=True`. Migration 0001 creates the six tables exactly as models declare (write by hand or autogenerate then review; the committed file must match models).

- [ ] Write models; `tests/test_migrations.py` (db): run `alembic upgrade head` programmatically against the owner URL on a truncated/dropped schema (`alembic.command.upgrade(cfg, "head")` in a thread via `asyncio.to_thread`), then assert the six tables exist (`SELECT tablename FROM pg_tables WHERE schemaname='public'`), plans seeded (`SELECT count(*) FROM plan == 2`), and `alembic check`/autogenerate diff is empty (`alembic.autogenerate.compare_metadata(...)` returns `[]`). Also assert `openmodel_app` can `INSERT` into `organization` (default privileges) via the app engine.
- [ ] conftest: after TRUNCATE, call `seed_plans(session)`. Truncate list from Task 1 stands.
- [ ] compose: `migrate` service: `build: ./api`, `command: ["alembic", "upgrade", "head"]`, env `OPENMODEL_DATABASE_OWNER_URL=...@postgres...`, `depends_on: {postgres: {condition: service_healthy}}`, `restart: "no"`; `api` adds `depends_on: {migrate: {condition: service_completed_successfully}}`. Dockerfile copies `alembic.ini` and `alembic/`.
- [ ] Verify: `docker compose up -d --build` → migrate exits 0, `docker compose exec postgres psql -U openmodel_app openmodel -c '\dt'` shows tables; pytest green.
- [ ] Commits: `feat: sqlalchemy models for the 3nf schema`; `feat: alembic initial migration and plan seed`; `chore: migrate service in compose`.

---

### Task 3: Key generation and admin endpoints

**Files:**
- Create: `api/app/auth/__init__.py`, `api/app/auth/keys.py`, `api/app/auth/admin.py`, `api/app/routes/admin.py`, `api/app/schemas/admin.py`, `api/tests/test_keys.py`, `api/tests/test_admin.py`
- Modify: `api/app/main.py` (include admin router), `api/app/errors.py` (+ `Unauthorized` 401 `invalid_request_error`/`invalid_api_key`, `Forbidden` 403 `invalid_request_error`/`forbidden`, `NotFound` 404 `invalid_request_error`/`not_found`, `Conflict` 409 `invalid_request_error`/`conflict`)

**Interfaces:**
- Produces `app/auth/keys.py`:
  ```python
  @dataclass(frozen=True)
  class GeneratedKey: raw: str; key_id: str; secret: str; prefix: str   # prefix = f"om_{env}_{key_id}"
  def generate_key(env: str) -> GeneratedKey
  def parse_key(raw: str) -> tuple[str, str] | None     # (key_id, secret) or None if malformed; regex ^om_[a-z]+_([A-Za-z0-9_-]{12})_([A-Za-z0-9_-]{32})$
  def hash_secret(secret: str, pepper: str) -> str      # argon2id via PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1); hashes secret + pepper
  def verify_secret(secret_hash: str, secret: str, pepper: str) -> bool   # never raises
  ```
- Produces `app/auth/admin.py`: `async def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None` — `hmac.compare_digest` against `settings.admin_token`, else `Unauthorized`.
- Produces `routes/admin.py` (prefix `/admin`, `dependencies=[Depends(require_admin)]`):
  - `GET /admin/plans` → `[{"code","requests_per_minute","max_concurrency","models":[...]}]`
  - `PUT /admin/plans/{code}` body `{requests_per_minute, max_concurrency, models: list[str]}` → upsert plan + replace plan_model rows.
  - `POST /admin/orgs` `{name, plan_code}` → 201 `{"id","name","plan_code","created_at"}`; duplicate name → 409; unknown plan → 404.
  - `GET /admin/orgs` → list.
  - `POST /admin/users` `{organization_id, email}` → 201 `{"id","organization_id","email","created_at"}`; unknown org 404; duplicate email 409.
  - `POST /admin/users/{user_id}/keys` → 201 `{"id","key":raw,"prefix","created_at"}` (raw shown once).
- Both `Unauthorized` responses set header `WWW-Authenticate: Bearer` (handled in `api_error_handler` when `exc.status_code == 401`).

- [ ] Tests `test_keys.py` (pure): generated raw parses back to the same key_id/secret; prefix format; two generations differ; `verify_secret` true/false; wrong pepper false; `parse_key` rejects `om_dev_short_x`, empty, missing parts.
- [ ] Tests `test_admin.py` (db): missing/wrong admin token → 401 envelope with `WWW-Authenticate`; create org/user/key happy path with shapes above; 404/409 paths; key row stored with `secret_hash` starting `$argon2id$` and not containing the secret; `GET /admin/plans` shows seeded plans; `PUT /admin/plans/free` changes rpm and models.
- [ ] Commits: `feat: api key generation and argon2 hashing`; `feat: admin endpoints for plans, orgs, users, keys`.

---

### Task 4: Bearer auth with Redis cache, protected routes, /api/keys

**Files:**
- Create: `api/app/auth/principal.py`, `api/app/routes/keys.py`, `api/app/schemas/keys.py`, `api/tests/test_auth.py`, `api/tests/test_api_keys.py`
- Modify: `api/app/main.py` (router dependencies), `api/app/routes/chat.py`, `completions.py`, `embeddings.py`, `models.py` (accept `Principal` dependency), `api/tests/conftest.py` (`principal_key` fixture: creates org(free)/user/key via the admin API and returns the raw key; `auth_client` fixture = client with `Authorization` header preset)

**Interfaces:**
- Produces `app/auth/principal.py`:
  ```python
  @dataclass(frozen=True)
  class Principal:
      api_key_id: UUID; user_id: UUID; organization_id: UUID
      plan_code: str; requests_per_minute: int; max_concurrency: int; models: frozenset[str]
  async def resolve_principal(raw_key: str, session: AsyncSession, redis: Redis) -> Principal   # raises Unauthorized
  async def require_principal(request: Request, authorization: Annotated[str | None, Header()] = None, session = Depends(get_session)) -> Principal
  def cache_key(key_id: str) -> str   # f"auth:{key_id}"
  async def drop_cached(redis, key_id) -> None
  ```
  Resolution (miss path): parse → load key by `key_id` (joined user→org→plan→plan_models) → reject if `revoked_at` → argon2 `verify_secret` → cache JSON `{principal fields, "fast_hash": sha256(secret + pepper)}` at `auth:{key_id}` with TTL `auth_cache_ttl_s`.
  Resolution (hit path): `hmac.compare_digest(sha256(secret + pepper), cached["fast_hash"])`; mismatch → `Unauthorized` (no DB fallback). Rationale: argon2 per request costs ~20ms; the sha256 lives only in Redis with a TTL, so a DB dump alone still yields only argon2 hashes.
- Produces `routes/keys.py` (prefix `/api/keys`, requires principal):
  - `POST /api/keys` → 201 `{"id","key","prefix","created_at"}` for the caller's user.
  - `GET /api/keys` → `{"data":[{"id","prefix","created_at","revoked_at"}]}` for the caller's user (revoked included).
  - `DELETE /api/keys/{id}` → 204; sets `revoked_at`, `drop_cached`; 404 if not the caller's; deleting the key currently in use is allowed.
- Protect: `app.include_router(chat.router, dependencies=[Depends(require_principal)])` etc. for chat, completions, embeddings, models, keys; routes that need the principal (Task 5/6) take `principal: Annotated[Principal, Depends(require_principal)]` — FastAPI dedupes the dependency per request.
- `/v1/models` now lists only models in `principal.models` ∩ registry.

- [ ] Tests `test_auth.py` (db + redis): no header → 401 envelope + `WWW-Authenticate: Bearer`; malformed key → 401; unknown key_id → 401; wrong secret → 401; valid → 200 on `/v1/models` filtered to plan models; second call is a cache hit (`redis_client.exists("auth:<key_id>")` and argon2 verify not called — monkeypatch `verify_secret` to raise after first call); revoked key → 401 and cache dropped; cache poisoning test: after cache fill, a request with the right key_id but wrong secret → 401.
- [ ] Tests `test_api_keys.py` (db): create/list/delete flow; delete other user's key → 404; deleted key → subsequent 401.
- [ ] Ensure Phase 1 route tests still pass: update `conftest.client` so non-db route tests get an app where `require_principal` is overridden (`app.dependency_overrides[require_principal] = lambda: TEST_PRINCIPAL` with a `free`-like principal including both test models). Db tests use the real dependency via `auth_client`.
- [ ] Commits: `feat: bearer api key auth with redis cache`; `feat: /api/keys self-service`; `test: protect phase 1 routes, principal override for unit tests`.

---

### Task 5: Rate limiting, concurrency cap, plan gating

**Files:**
- Create: `api/app/ratelimit.py`, `api/tests/test_ratelimit.py`
- Modify: `api/app/errors.py` (+ `RateLimited` 429 `rate_limit_error`/`rate_limit_exceeded` with `retry_after: int`; `ModelNotAllowed` 403 `invalid_request_error`/`model_not_allowed`), `api/app/main.py` (429 handler sets `Retry-After` and `X-RateLimit-*` headers), `api/app/routes/shared.py` (`backend_for` gains `principal` and raises `ModelNotAllowed` when `model not in principal.models` — checked **after** `registry.require` so unknown models stay 404), chat/completions/embeddings routes (enforce dependency + concurrency slot around the backend call / stream).

**Interfaces:**
- Produces `app/ratelimit.py`:
  ```python
  TOKEN_BUCKET_LUA = """
  local key = KEYS[1]; local rate = tonumber(ARGV[1]); local burst = tonumber(ARGV[2]); local now = tonumber(ARGV[3])
  local data = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(data[1]); local ts = tonumber(data[2])
  if tokens == nil then tokens = burst; ts = now end
  tokens = math.min(burst, tokens + (now - ts) * rate)
  local allowed = 0
  if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
  redis.call('HSET', key, 'tokens', tokens, 'ts', now)
  redis.call('PEXPIRE', key, math.ceil(burst / rate * 1000) + 1000)
  local retry_ms = 0
  if allowed == 0 then retry_ms = math.ceil((1 - tokens) / rate * 1000) end
  return {allowed, math.floor(tokens), retry_ms}
  """
  @dataclass(frozen=True)
  class Decision: allowed: bool; remaining: int; limit: int; retry_after_s: int
  async def take_token(redis, org_id: UUID, rpm: int, now: float | None = None) -> Decision   # rate = rpm/60 per second, burst = rpm, key f"rl:{org_id}"
  class ConcurrencySlot:  # async context manager
      def __init__(self, redis, org_id: UUID, limit: int) -> None
      # __aenter__: INCR f"cc:{org_id}"; if > limit: DECR and raise RateLimited(retry_after=1, reason="concurrency"); set EXPIRE 300 as a leak guard
      # __aexit__: DECR (never below 0: use a small Lua or `DECR` then `if < 0: SET 0`)
  async def enforce(request, principal = Depends(require_principal)) -> Principal   # dependency: take_token; raise RateLimited; stash Decision on request.state.rate for headers
  ```
- `RateLimited(ApiError)` carries `retry_after_s`, `limit`, `remaining`; `api_error_handler` sets `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`. Successful responses also get `X-RateLimit-Limit/Remaining` via a tiny middleware or by setting them in `enforce` on `request.state` and adding in `RequestIdMiddleware` (choose the latter: one place already touches response headers).
- Routes: `enforce` replaces `require_principal` in the LLM routers' dependencies; sync paths wrap the backend call in `ConcurrencySlot`; `stream_events` takes an optional `slot: ConcurrencySlot | None` and enters it around the generator body (so the slot is held for the stream's lifetime and released on disconnect/error).

- [ ] Tests `test_ratelimit.py` (redis): `take_token` with rpm=60: 60 immediate allows, 61st denied with `retry_after_s == 1`; with `now` injected, refill after 1s allows one more; concurrency: two entered slots at limit 2, third raises, exit releases; `/v1/chat/completions` under a plan with rpm=2 → third call 429 envelope with `Retry-After` and `X-RateLimit-Remaining: 0`; 200 responses carry `X-RateLimit-*`; model not in plan → 403 `model_not_allowed`; unknown model still 404; streaming holds the slot until the stream ends (start a stream with a slow respx generator, assert `cc:{org}` == 1 mid-stream, 0 after).
- [ ] Commits: `feat: redis token bucket and concurrency cap`; `feat: enforce plan limits and model gating on llm routes`.

---

### Task 6: Usage metering and /api/usage

**Files:**
- Create: `api/app/usage.py`, `api/app/routes/usage.py`, `api/app/schemas/usage.py`, `api/tests/test_usage.py`
- Modify: `api/app/routes/shared.py`, chat/completions/embeddings (record usage), `api/app/main.py` (router)

**Interfaces:**
- Produces `app/usage.py`:
  ```python
  @dataclass(frozen=True)
  class UsageRecord: api_key_id: UUID; model_name: str; endpoint: str; prompt_tokens: int; completion_tokens: int; latency_ms: int; status_code: int
  async def write(sessionmaker, record: UsageRecord) -> None        # own session, commit; log and swallow exceptions (never fail the request)
  def schedule(request: Request, record: UsageRecord) -> None        # asyncio.create_task(write(...)); keep a reference set on app.state.usage_tasks to avoid GC; discard on done
  async def aggregate(session, organization_id: UUID, since: datetime, until: datetime, group_by: Literal["model","user","day"] | None) -> list[dict]
  ```
  Aggregation SQL joins `request → api_key → app_user` filtered by `organization_id`, `created_at >= since AND < until`; returns rows `{key, requests, prompt_tokens, completion_tokens}` where `key` is the model name, user email, or `date_trunc('day', created_at)` ISO date; without group_by one totals row.
- Recording points: sync chat/completions/embeddings after backend returns (status 200) and on `ApiError` from the backend (status = exc.status_code, tokens 0); streaming: in `stream_events` after the final delta (status 200, tokens from usage) or in-stream error (status = exc.status_code); disconnect mid-stream records status 499 with tokens seen so far (0 if no usage delta). Latency = wall time from route entry.
- Produces `GET /api/usage?from=&to=&group_by=` (requires principal): defaults `from = now - 30d`, `to = now`; response `{"organization_id", "from", "to", "requests", "prompt_tokens", "completion_tokens", "groups": [...]}` (`groups` empty when no group_by). Invalid `group_by` → 422 envelope (Literal validation).

- [ ] Tests `test_usage.py` (db + redis): after one chat call via `auth_client`, one `request` row exists with correct tokens/model/endpoint/status and `api_key_id`; a 502 backend failure records status 502 with zero tokens; a stream records tokens from the final chunk; `/api/usage` totals and each `group_by` shape; org isolation (a second org's key sees zero); `write` swallowing a DB error (monkeypatch sessionmaker to raise) does not affect the response.
- [ ] Commits: `feat: usage metering background writer`; `feat: /api/usage aggregation`.

---

### Task 7: Runbook, e2e, live verification

**Files:**
- Create: `docs/runbooks/02-platform-layer.md`
- Modify: `api/tests/test_e2e.py`, `README.md` (phase table row 2 done, quickstart with admin bootstrap), `docs/runbooks/01-gateway-core.md` (note that requests now need a key)

- [ ] e2e: bootstrap via admin API (`X-Admin-Token: dev-admin-token` at `OPENMODEL_BASE_URL` origin): create org `e2e-<uuid>` on `pro`, user, key; use the raw key as `api_key` for the `openai` client; keep the five Phase 1 assertions; add: no key → 401; `GET /api/usage` after the calls shows `requests >= 4`; `qwen2.5-coder:0.5b` is listed for pro (it need not be pulled; only `/v1/models` is asserted).
- [ ] Runbook 02: bootstrap commands (curl admin endpoints), create a key, hit the limit (loop 61 requests with a `free` org, show 429 headers), read usage, inspect tables with `psql` (join query showing 3NF), inspect Redis keys (`auth:*`, `rl:*`, `cc:*`), break-it: stop redis → requests 503? Decide: `enforce` treats Redis errors as **fail-closed 503** `server_error`/`rate_limiter_unavailable` (document), stop postgres → auth cache keeps working for TTL then 503 `database_unavailable` (map `sqlalchemy.exc.OperationalError` in a handler). Implement those two mappings here in `errors.py` + `main.py` if not already, with tests.
- [ ] Verify live: `OPENMODEL_HOST_PORT=8010 docker compose up -d --build` → migrate ok → `uv run pytest -m e2e` 5+3 pass → paste output.
- [ ] Commits: `fix: fail closed when redis or postgres are unavailable`; `test: e2e bootstrap with admin api and real key`; `docs: runbook 02 platform layer`.

---

## Self-review

- Spec coverage: schema 3NF ✔ (T2), key format/argon2/pepper/cache/revoke ✔ (T3, T4), admin surface ✔ (T3), plans table + plan_model + `/admin/plans` ✔ (T2, T3), rate limit token bucket + concurrency + 429 headers ✔ (T5), plan gating 403 ✔ (T5), usage rows + `/api/usage` group_by ✔ (T6), roles/timeouts/config from Postgres ops ✔ (T1), pool budget 3+2 ✔ (T1), pg_stat_statements ✔ (T1), migrate Job pattern previewed as compose service ✔ (T2). Deferred to Phase 3: initContainer wait, imagePullSecret. Deferred to Phase 4: postgres-exporter.
- Placeholder scan: none.
- Type consistency: `Principal` fields used by T5 (`requests_per_minute`, `max_concurrency`, `models`, `organization_id`) and T6 (`api_key_id`) match T4. `RateLimited` fields match handler. `get_session` from T1 used by T3-T6.

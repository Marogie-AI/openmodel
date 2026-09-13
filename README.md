# OpenModel

An OpenAI-compatible inference platform you host yourself. A FastAPI gateway puts
API keys, per-organization rate limits, plan-based model gating and usage metering
in front of open-source models, and a browser console drives all of it. Runs on
Docker Compose in one command, or on Kubernetes behind Envoy Gateway.

Clients talk to it with the official `openai` SDK by pointing `base_url` at it.

```
browser ─────────────────────────┐
                                 │ /app  (console, static, same origin)
client ──HTTPS──▶ Envoy Gateway ─┤
   openai SDK                    │ /v1 /api /admin
                                 ▼
                   api Deployment (autoscaled 2..10 on in-flight requests)
                   auth → rate limit → plan gate → model router → usage
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
     Postgres                  Redis            Ollama per model
   orgs, keys, usage      key cache, limits     qwen2.5 · qwen2.5-coder
```

## Layout

| Path | What |
|---|---|
| `apps/backend` | FastAPI gateway, Alembic migrations, tests |
| `apps/frontend` | React console, built into the backend image |
| `kubernetes` | Kustomize base and overlays, kind config, Helm values |
| `scripts` | Cluster lifecycle, secrets, deploy, k6 load tests |

The console is served as static files by the API pod itself, so it shares an
origin with the API and the backend needs no CORS middleware.

## Quickstart

```sh
docker compose up -d --build
```

Nothing has an account yet, so make one. Admin endpoints are gated by a shared
token rather than an API key:

```sh
export ADMIN='X-Admin-Token: dev-admin-token-change-me'
export JSON='content-type: application/json'

ORG=$(curl -s -X POST localhost:8000/admin/orgs -H "$ADMIN" -H "$JSON" \
  -d '{"name":"acme","plan_code":"pro"}' | jq -r .id)

USER=$(curl -s -X POST localhost:8000/admin/users -H "$ADMIN" -H "$JSON" \
  -d "{\"organization_id\":\"$ORG\",\"email\":\"dev@acme.test\"}" | jq -r .id)

KEY=$(curl -s -X POST localhost:8000/admin/users/$USER/keys -H "$ADMIN" | jq -r .key)
```

That response is the only time the raw key is readable. Only its prefix and an
argon2id hash are stored, so it cannot be recovered afterwards.

Open **http://localhost:8000/app/** and paste the key on the Setup tab. Or use it
from the command line like any OpenAI endpoint:

```sh
curl -s localhost:8000/v1/models -H "Authorization: Bearer $KEY"

curl -s localhost:8000/v1/chat/completions -H "Authorization: Bearer $KEY" -H "$JSON" \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Say hi."}]}'

curl -s localhost:8000/api/usage -H "Authorization: Bearer $KEY"
```

If port 8000 is taken: `OPENMODEL_HOST_PORT=8010 docker compose up -d`.

## The console

Five tabs at `/app`, served by the API pod, no build step to run separately.

| Tab | Does |
|---|---|
| Setup | Validates a key, then shows your organization, plan, limits and permitted models |
| Chat | Streams completions with a model picker, stop button, token counts and remaining rate budget |
| Keys | Mints, lists and revokes your own keys. Shows a new secret exactly once |
| Usage | Requests and tokens for your organization, grouped by model, user or day |
| Admin | Creates organizations and users behind the admin token, which is never persisted |

To work on it with hot reload, run the backend from compose and the console from
Vite. The dev server proxies `/v1`, `/api` and `/admin` to the backend, which is
why no CORS configuration exists anywhere:

```sh
cd apps/frontend && bun install && bun dev
```

## On Kubernetes

The same gateway on a one-node kind cluster behind Envoy Gateway over HTTPS. Stop
compose first with `docker compose down`, add this to `/etc/hosts`:

```
127.0.0.1 api.openmodel.test
```

then, from the repo root:

```sh
scripts/cluster-up.sh   # kind + Calico + Envoy Gateway + cert-manager + monitoring
scripts/secrets.sh      # generates the git-ignored dev secrets
scripts/deploy.sh       # builds the image, kind-loads it, applies the dev overlay

curl -k https://api.openmodel.test/ready
```

`-k` because the certificate is self-signed by an in-cluster issuer. Only `/v1`,
`/api`, `/admin`, `/app`, `/health` and `/ready` are routed at the edge. Anything
else, including `/metrics`, stays inside the cluster.

The acceptance suite runs through the gateway the same way:

```sh
ADMIN_TOKEN=$(grep '^OPENMODEL_ADMIN_TOKEN=' kubernetes/overlays/dev/secrets/api.env | cut -d= -f2)

cd apps/backend && OPENMODEL_BASE_URL=https://api.openmodel.test/v1 \
  OPENMODEL_INSECURE_TLS=1 OPENMODEL_ADMIN_TOKEN="$ADMIN_TOKEN" uv run pytest -m e2e
```

## Watch it

Prometheus, Grafana and the autoscaler come up with the cluster. Reach them by
port-forward, since they are deliberately not exposed at the edge:

```sh
kubectl -n openmodel-monitoring port-forward svc/kps-grafana 3000:80
# user admin, password from kubernetes/overlays/dev/secrets/grafana.env
```

Dashboard uid `openmodel` shows in-flight requests by model, time to first token,
tokens per second, error rate, and pod CPU and memory. Alongside it:

```sh
kubectl get hpa api -n openmodel-api -w   # scales on average in-flight requests
kubectl top pods -A                       # what is actually eating the node
```

Then put load on it with a `pro` key:

```sh
k6 run -e INSECURE=1 -e API_KEY="$KEY" scripts/load-test/k6.js
```

Watch the API replicas climb while tokens per second does not. The gateway is not
the bottleneck, the model server is, and scaling the first does nothing for the
second.

## Running it

Checks, from the repo root:

```sh
cd apps/backend  && uv run ruff check . && uv run mypy --strict app && uv run pytest
cd apps/frontend && bun run typecheck && bun run lint && bun test
```

Memory is the real constraint. Give Docker Desktop 12 GB if you can. At 8 GB the
monitoring stack and two model servers do not fit at once under load: the
acceptance suite passes fully with Prometheus and Grafana scaled to zero, and
fails about half its cases with them running, because Ollama runs out of room and
the kubelet starts timing out liveness probes.

| Component | Memory request |
|---|---|
| api, per pod | 128 Mi |
| Postgres | 512 Mi |
| Redis | 64 Mi |
| Ollama, per model | 768 Mi to 1 Gi |
| Prometheus and Grafana | 630 Mi observed |

Compose alone, without Kubernetes, runs comfortably in about 2 GB.

## Phases

| # | Phase | Delivers |
|---|---|---|
| 1 | Gateway core | Models, chat, completions, embeddings, sync and streaming, Ollama backend, metrics, compose |
| 2 | Platform layer | Postgres schema, argon2 API keys, admin surface, rate limits, usage metering |
| 3 | Cluster | kind, namespaces with Pod Security Admission, Kustomize, StatefulSets, Gateway API, NetworkPolicies, TLS |
| 4 | Ops and scale | Prometheus, Grafana, alerts, autoscaling on in-flight requests, k6, a second model |
| 4.5 | Console | The five-tab browser console, served from the API pod |
| 5 | Delivery | GitHub Actions, image publishing, chaos drills |

Specs, plans and runbooks are kept outside version control, so they are not in
this repository.

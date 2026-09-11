# OpenModel API

OpenAI-compatible inference platform on Kubernetes. Built to learn the stack:
FastAPI gateway, Ollama backends, Postgres, Redis, kind + Calico + Envoy Gateway,
Prometheus/Grafana, HPA, CI.

```
client ──HTTPS api.openmodel.test──▶ Envoy Gateway
                                        ▼
                             api Deployment (HPA 2..10)
                             auth → ratelimit → router → backend → usage
                                        ▼
              postgres            redis            ollama-qwen / ollama-coder
```

## Phases

| # | Phase | Delivers |
|---|---|---|
| 1 | Gateway core ✅ | FastAPI: models, chat, completions, embeddings (sync+SSE), Ollama backend, metrics, compose |
| 2 | Platform layer ✅ | Postgres schema, API keys, admin, rate limit, usage |
| 3 | Cluster ✅ | kind, namespaces, Kustomize, StatefulSets, Gateway, NetworkPolicy, TLS |
| 4 | Ops + scale | Prometheus, Grafana, HPA on in-flight requests, k6, second model |
| 5 | Delivery + chaos | GitHub Actions, GHCR, kind e2e, chaos runbook |

## Quickstart

```sh
docker compose up -d --build
```

The stack ships with two plans and no accounts, so make one. The admin
endpoints are gated by a shared token, not by an API key:

```sh
export ADMIN='X-Admin-Token: dev-admin-token-change-me'
export JSON='content-type: application/json'

ORG=$(curl -s -X POST localhost:8000/admin/orgs -H "$ADMIN" -H "$JSON" \
  -d '{"name":"acme","plan_code":"free"}' | jq -r .id)

USER=$(curl -s -X POST localhost:8000/admin/users -H "$ADMIN" -H "$JSON" \
  -d "{\"organization_id\":\"$ORG\",\"email\":\"dev@acme.test\"}" | jq -r .id)

KEY=$(curl -s -X POST localhost:8000/admin/users/$USER/keys -H "$ADMIN" | jq -r .key)
```

That response is the only time the raw key is readable — only its prefix and an
argon2id hash are stored. Then call the gateway like any OpenAI endpoint:

```sh
curl -s localhost:8000/v1/models -H "Authorization: Bearer $KEY"

curl -s localhost:8000/v1/chat/completions -H "Authorization: Bearer $KEY" -H "$JSON" \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Say hi."}]}'

curl -s localhost:8000/api/usage -H "Authorization: Bearer $KEY"
```

If port 8000 is taken: `OPENMODEL_HOST_PORT=8010 docker compose up -d`.

## On Kubernetes

Same gateway, on a one-node kind cluster behind Envoy Gateway over HTTPS. Stop
compose first (`docker compose down`), add this to `/etc/hosts`:

```
127.0.0.1 api.openmodel.test
```

then, from the repo root:

```sh
scripts/cluster-up.sh   # kind + Calico + Envoy Gateway + cert-manager
scripts/secrets.sh      # generates the git-ignored dev secrets
scripts/deploy.sh       # builds the image, kind-loads it, applies the dev overlay

curl -k https://api.openmodel.test/ready
```

`-k` because the certificate is self-signed by an in-cluster issuer. The
acceptance suite runs through the gateway the same way:

```sh
ADMIN_TOKEN=$(grep '^OPENMODEL_ADMIN_TOKEN=' kubernetes/overlays/dev/secrets/api.env | cut -d= -f2)

cd api && OPENMODEL_BASE_URL=https://api.openmodel.test/v1 \
  OPENMODEL_INSECURE_TLS=1 OPENMODEL_ADMIN_TOKEN="$ADMIN_TOKEN" uv run pytest -m e2e
```

Memory: give Docker Desktop 12 GB if you can. 8 GB works with one model pulled
and no monitoring stack, but a second model or another project's containers
will push Ollama into `model requires more system memory` and chat requests
will 502 while embeddings keep working.

Design: [docs/specs/2026-09-10-openmodel-design.md](docs/specs/2026-09-10-openmodel-design.md).
Runbooks: [gateway core](docs/runbooks/01-gateway-core.md),
[platform layer](docs/runbooks/02-platform-layer.md),
[cluster](docs/runbooks/03-cluster.md).

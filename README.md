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
| 1 | Gateway core | FastAPI: models, chat, completions, embeddings (sync+SSE), Ollama backend, metrics, compose |
| 2 | Platform layer | Postgres schema, API keys, admin, rate limit, usage |
| 3 | Cluster | kind, namespaces, Kustomize, StatefulSets, Gateway, NetworkPolicy, TLS |
| 4 | Ops + scale | Prometheus, Grafana, HPA on in-flight requests, k6, second model |
| 5 | Delivery + chaos | GitHub Actions, GHCR, kind e2e, chaos runbook |

Design: [docs/specs/2026-09-10-openmodel-design.md](docs/specs/2026-09-10-openmodel-design.md).
Runbooks: `docs/runbooks/`.

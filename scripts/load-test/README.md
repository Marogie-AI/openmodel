# Load test

`k6.js` drives the gateway the way a client would: streaming chat, sync chat and
embeddings at the same time, through `https://api.openmodel.test`. It is what
the HPA and the dashboard are for — run it with Grafana open.

## Mint a pro key

The load profile needs the pro plan: 600 requests a minute and 10 concurrent
requests. Read the admin token out of the git-ignored secrets file rather than
pasting it:

```sh
ADMIN_TOKEN=$(grep '^OPENMODEL_ADMIN_TOKEN=' kubernetes/overlays/dev/secrets/api.env | cut -d= -f2)
ADMIN="X-Admin-Token: $ADMIN_TOKEN"
JSON='content-type: application/json'

ORG=$(curl -sk -X POST https://api.openmodel.test/admin/orgs -H "$ADMIN" -H "$JSON" \
  -d '{"name":"loadtest","plan_code":"pro"}' | jq -r .id)

USER=$(curl -sk -X POST https://api.openmodel.test/admin/users -H "$ADMIN" -H "$JSON" \
  -d "{\"organization_id\":\"$ORG\",\"email\":\"load@test.dev\"}" | jq -r .id)

K6_KEY=$(curl -sk -X POST https://api.openmodel.test/admin/users/$USER/keys -H "$ADMIN" | jq -r .key)
```

That response is the only time the raw key is readable.

## Run it

```sh
k6 run -e INSECURE=1 -e API_KEY="$K6_KEY" scripts/load-test/k6.js
```

Sixty seconds of three scenarios at once:

| scenario | shape | why |
|---|---|---|
| `chat_stream` | 3 VUs, `stream: true`, `max_tokens: 64` | the in-flight load the HPA scales on |
| `chat_sync` | 1 request/s arrival rate, `max_tokens: 32` | latency under queueing |
| `embeddings` | 2 requests/s arrival rate | the cheap path that should stay fast |

Peak concurrency is 8, under the pro plan's cap of 10 — and, more to the point,
close to what Ollama can actually serve (`OLLAMA_NUM_PARALLEL=2` per backend).
The first version of this file ran 6 + 4 + 2 and failed 99% of its requests:
streaming requests hold their concurrency slot for the whole generation, so once
they queue on Ollama the org sits at its cap and every other request gets a
`429`, which k6 scores as a failure. If you see that, lower the VUs — raising the
plan only moves the queue.

Other knobs: `BASE_URL` (default `https://api.openmodel.test`), `INSECURE=1` for
the self-signed certificate, `CHAT_MODEL` / `EMBED_MODEL`, and
`ONLY=chat_stream` to run a single scenario — that is how the runbook compares
`OLLAMA_NUM_PARALLEL` settings without the other traffic in the way.

## Read the summary

stdout ends with the numbers that matter. This is a real run of this profile, on
an 8 GB Docker Desktop with the model already loaded and swap exhausted — an
example of a *bad* run, kept because it is the one that was measured:

```
requests          9.00 (0.13/s)
failed           78.00%
duration avg    61678.67 ms
duration p(95)  70344.09 ms
checks passed    4.00 / failed 14.00
```

Nine requests in ninety seconds at a minute each: the backend, not the gateway.
A healthy run on a host with headroom looks like a few hundred requests, `failed`
at 0%, `p(95)` a few seconds, every check passing, and `dropped iters` at 0.

Thresholds: `http_req_failed < 5%` and `http_req_duration p(95) < 10s`. k6 exits
non-zero when either is crossed.

`dropped iters` is printed but deliberately *not* a threshold. The two
arrival-rate scenarios hold a fixed request rate; when the backend cannot keep
up, k6 adds VUs to `maxVUs` and then drops the iterations it cannot start. A
non-zero count therefore means "the backend could not sustain the offered rate",
which is a finding about the cluster, not a broken test — failing the run on it
would only hide the number.

The whole k6 summary object is also written to
`scripts/load-test/summary.json` (git-ignored) — use it to diff runs:

```sh
jq '.metrics.http_req_duration.values["p(95)"]' scripts/load-test/summary.json
```

## Where to look in Grafana

```sh
kubectl -n openmodel-monitoring port-forward svc/kps-grafana 3000:80
```

Dashboard `openmodel` (uid `openmodel`). During a run, watch in this order:

- **in-flight by model** — climbs to the VU count, then flattens at whatever the
  model pod can hold. That flat line is the queue forming.
- **TTFT p95** — rises while in-flight is flat: requests are waiting, not working.
- **tokens/s** — stays roughly constant no matter how much load you add. The
  bottleneck's throughput, not the gateway's.
- **API pod count / restarts** — the HPA adding replicas that do not help.

`kubectl get hpa api -n openmodel-api -w` in another terminal tells the same
story faster. The full explanation is in
[docs/runbooks/04-ops-scale.md](../../docs/runbooks/04-ops-scale.md).

# Runbook 04 — Ops and scale

Phase 3 left a cluster you can break and repair. Phase 4 adds the instruments:
Prometheus scraping the api, Postgres and Envoy, Grafana with one dashboard,
seven alert rules, and an HPA that scales the api Deployment on in-flight LLM
requests instead of CPU. This runbook is about reading those instruments while
something is actually happening, and about the one lesson the whole phase exists
to teach: **scaling the API is not scaling the bottleneck.**

Every command below was run once against the dev cluster. The output under each
is the real thing, trimmed. The machine was a 10-core laptop with Docker Desktop
at 8 GB, running other projects' containers at the same time — that turns out to
matter, and the numbers say so rather than being cleaned up.

## Watch it

Prometheus and Grafana are not on the gateway. Reach them by port-forward, in
two terminals you leave open:

```sh
kubectl -n openmodel-monitoring port-forward svc/kps-grafana 3000:80
kubectl -n openmodel-monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090
```

Grafana is `admin` plus the password in
`kubernetes/overlays/dev/secrets/grafana.env`; the dashboard is uid `openmodel`.
Prometheus at `localhost:9090` is the faster tool when you want one number —
`/alerts` for rule state, `/targets` for what is being scraped.

Two more terminals earn their place during a load test:

```sh
kubectl get hpa api -n openmodel-api -w   # replicas and the metric they follow
kubectl top pods -A                       # what is actually eating the node
```

`kubectl top` comes from metrics-server and shows CPU and memory per pod. The
HPA does *not* use it: `llm_inflight` reaches the HPA through
prometheus-adapter under `custom.metrics.k8s.io`. Two metric pipelines, two
different failure modes — if `kubectl top` is empty, metrics-server is unwell;
if the HPA shows `<unknown>`, the adapter or the api `/metrics` endpoint is.

```sh
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1/namespaces/openmodel-api/pods/*/llm_inflight | jq .
```

## Load it

The profile lives in `scripts/load-test/` — how to mint a pro key is in the
README there.

```sh
k6 run -e INSECURE=1 -e API_KEY="$K6_KEY" scripts/load-test/k6.js
```

### The first run failed, and the failure is the lesson

The profile originally ran 6 streaming VUs, 2 sync chats/s and 5 embeddings/s —
12 possible concurrent requests against a pro plan that allows 10. It looked
safe. It was not:

```
requests        223.00 (2.48/s)
failed          99.00%
duration avg    731.49 ms
duration p(95)  884.19 ms
checks passed   6.00 / failed 440.00
```

Ninety-nine percent failed in under a second each. Prometheus says what they
were:

```
sum by (status) (increase(http_requests_total{job="api"}[20m]))

429   193.8
503    27.4
200   778.8      # includes /ready and /metrics scrapes
```

`429`, not `500`. A streaming request holds its concurrency slot
(`ConcurrencySlot` in `api/app/ratelimit.py`) for the whole generation. Ollama
serves `OLLAMA_NUM_PARALLEL=2` at a time, so six streams take tens of seconds
each, six slots stay held, the sync scenario takes the rest — and every
embedding request, which would have finished in 200 ms, is rejected at the door.

The fix is to lower the load, not to raise the plan: a bigger cap would only let
more requests queue on the same two Ollama slots. The committed profile is 3
streams + 1 sync/s + 2 embeddings/s, peak 8 in flight.

### The second run: the host is the bottleneck

```
requests          9.00 (0.13/s)
failed           78.00%
duration avg    61678.67 ms
duration p(95)  70344.09 ms
checks passed    4.00 / failed 14.00
```

Nine requests in ninety seconds, a minute each. Inside the node:

```
docker exec openmodel-control-plane free -m
               total        used        free      shared  buff/cache   available
Mem:            7837        6970         152          78         991         867
Swap:           1023        1023           0
```

Swap fully consumed, 150 MB free. Ollama does CPU inference; when the page cache
it needs is on swap, generation slows by an order of magnitude, and the same
pressure makes the kubelet's liveness probes time out. During the first run the
API server itself became unreachable for two minutes (`kubectl: net/http: TLS
handshake timeout`, load average 89) and half the control plane restarted:
kube-scheduler, kube-controller-manager, metrics-server, kube-state-metrics,
cert-manager, envoy-gateway. Nothing was evicted and nothing was OOMKilled —
it thrashed instead.

That is the honest state of an 8 GB Docker Desktop running two Ollama
deployments, the monitoring stack, and another project's containers. On 12 GB
the same profile is unremarkable. Read every latency number below with that in
mind: the *shape* is real, the absolute values are a small laptop's.

## The bottleneck lesson

Watch the HPA through a run. It does exactly what it was asked to:

```
kubectl get hpa api -n openmodel-api -w

NAME   REFERENCE        TARGETS   MINPODS   MAXPODS   REPLICAS   AGE
api    Deployment/api   0/2       2         10        2          54m
api    Deployment/api   5/2       2         10        2          54m
api    Deployment/api   4500m/2   2         10        5          54m
api    Deployment/api   5/2       2         10        5          55m
api    Deployment/api   2/2       2         10        5          55m
api    Deployment/api   1800m/2   2         10        5          55m
```

Five in flight per pod against a target of 2, so the controller went 2 → 5 pods
in one step. Now look at what those five pods bought, from Prometheus:

```
                                                       before      during
sum(llm_inflight) by (model)
  {model="qwen2.5:0.5b"}                                    0          10
  {model="nomic-embed-text"}                                0           0
  {model="qwen2.5-coder:0.5b"}                              0           0

histogram_quantile(0.95, sum by (le,model)(rate(llm_ttft_seconds_bucket[1m])))
  {model="qwen2.5:0.5b"}                                  NaN          10

sum(rate(llm_tokens_total{direction="completion"}[1m]))
  {}                                                        0           0
```

`NaN` before the run is correct — no observations in the window, so no
quantile. The three numbers to read are: in-flight 10, TTFT p95 10, tokens/s 0.

In-flight is flat at the cap: ten requests are being *held*, not served. TTFT
p95 sits in the top histogram bucket (10s is the last boundary), meaning
requests are waiting rather than generating. And completion tokens per second —
the only number that measures *work done* — stayed at zero through a run with
five api pods, because every stream was still waiting for its first token when
the run ended. Adding pods added holders of open sockets.

Three api pods forwarding to one Ollama is three queues in front of one kitchen.

### The `OLLAMA_NUM_PARALLEL` experiment

The knob that actually moves throughput is on the backend. Run the streaming
scenario alone at each setting, so nothing else is in the way:

```sh
kubectl -n openmodel-inference set env deploy/ollama OLLAMA_NUM_PARALLEL=1
kubectl -n openmodel-inference rollout status deploy/ollama
ONLY=chat_stream STREAM_VUS=2 k6 run -e INSECURE=1 -e API_KEY="$K6_KEY" scripts/load-test/k6.js
```

Three numbers were supposed to come out of this: TTFT p95 and completion
tokens/s at `OLLAMA_NUM_PARALLEL=1`, `=4`, and back at the committed `=2`. On
this host they did not, and the reason is worth more than the numbers would have
been.

At `=1`, with two streaming VUs and `max_tokens` cut to 8, not one iteration
finished inside the 60-second scenario plus 30-second graceful stop:

```
running (1m30.5s), 0/2 VUs, 0 complete and 2 interrupted iterations

sum(llm_inflight) by (model)                    {model="qwen2.5:0.5b"}  2
histogram_quantile(0.95, ... llm_ttft_seconds)  NaN     # no first token yet
sum(rate(llm_tokens_total{direction="completion"}[1m]))  0
```

`NaN` is not a broken query: `llm_ttft_seconds` is observed when the *first*
token arrives (`api/app/routes/shared.py`), and in ninety seconds no request got
one. A single hand-timed stream confirmed it:

```sh
time curl -sk -N -X POST https://api.openmodel.test/v1/chat/completions ... max_tokens 64
# 3:00.81 total   — 64 tokens, about 0.35 tokens/second
```

The same request took 15.6 s earlier the same afternoon, before the load runs
filled swap. Nothing in the cluster changed in between — `OLLAMA_NUM_PARALLEL`
was still 2 for that measurement — so the variable that moved was free memory on
the host, not anything Kubernetes can see:

```
Mem:  7837 total, 149 free, 1381 buff/cache, 1253 available
Swap: 1023 total, 1023 used, 0 free
```

Ollama does CPU inference from a memory-mapped model. With swap full and ~150 MB
free, the model's pages are evicted between tokens and every token becomes a
disk read. The generation rate collapses by two orders of magnitude, and with it
any chance of comparing a parallelism setting.

Worse, the `=1` run's second attempt did not merely go slow, it inverted:

```
requests        11363.00 (189.37/s)
failed          100.00%
duration avg    10.00 ms
```

Eleven thousand requests in a minute, all failing in 10 ms. With one Ollama slot
and two VUs, the api pods' readiness probes (which check the backend) started
timing out; both pods went `0/1 Ready`, Envoy had no endpoints, and every k6
iteration got an instant 503 — so k6, freed from waiting on the model, hammered
the gateway 190 times a second. A saturated backend can turn a load test into a
different load test.

**What to take from it.** The experiment is sound and the command sequence below
is correct; it needs a host with headroom (12 GB Docker Desktop, nothing else
running), where `=1` should roughly halve throughput versus `=2` and push TTFT
p95 up, and `=4` should raise tokens/s slightly while TTFT p95 rises further
because four requests now share the same cores and KV cache. Run it there:

```sh
for n in 1 4 2; do
  kubectl -n openmodel-inference set env deploy/ollama OLLAMA_NUM_PARALLEL=$n
  kubectl -n openmodel-inference rollout status deploy/ollama
  ONLY=chat_stream STREAM_VUS=2 k6 run -e INSECURE=1 -e API_KEY="$K6_KEY" scripts/load-test/k6.js
  # read TTFT p95 and tokens/s off the dashboard, or:
  curl -sG localhost:9090/api/v1/query --data-urlencode \
    'query=histogram_quantile(0.95, sum by (le,model)(rate(llm_ttft_seconds_bucket[2m])))'
  curl -sG localhost:9090/api/v1/query --data-urlencode \
    'query=sum(rate(llm_tokens_total{direction="completion"}[2m]))'
done
```

The loop ends at `=2`, which is what `kubernetes/base/inference/ollama.yaml`
sets, so the cluster is left as committed. Confirm it:

```sh
kubectl -n openmodel-inference get deploy ollama \
  -o jsonpath='{.spec.template.spec.containers[0].env}' | tr ',' '\n' | grep PARALLEL
# {"name":"OLLAMA_NUM_PARALLEL","value":"2"}
```

And while you are in there, know the third number that is not `OLLAMA_NUM_PARALLEL`:
`kubectl top pod -n openmodel-inference` against the container's 2 CPU limit. If
Ollama is pinned at its limit, parallelism only decides how the same cores are
shared out — the queue moves, the throughput does not.

## Model scaling experiment

If one Ollama is the bottleneck, run two:

```sh
kubectl -n openmodel-inference scale deploy/ollama --replicas=2
kubectl -n openmodel-inference get pods -l app.kubernetes.io/name=ollama
```

The expectation going in was that the second pod would sit `Pending`: the model
PVC is `ReadWriteOnce`.

```
NAME                      READY   STATUS    RESTARTS   AGE
ollama-79dbd4c556-5rnv4   1/1     Running   0          28m
ollama-79dbd4c556-sndzx   1/1     Running   0          38s

kubectl -n openmodel-inference get pvc
NAME                  STATUS   CAPACITY   ACCESS MODES   STORAGECLASS
ollama-models         Bound    10Gi       RWO            standard
```

It did not. ReadWriteOnce means **one node**, not one pod: kind is a single-node
cluster, so both pods land on the same node and the kubelet mounts the same
volume into both. The only complaint was a startup probe timing out once while
the second Ollama loaded under memory pressure:

```
Warning  Unhealthy  2s  kubelet  Startup probe failed: Get "http://192.168.120.25:11434/api/tags":
                                 context deadline exceeded
```

So the RWO lesson is real but conditional: add a second node (or any real CSI
storage that only attaches to one node at a time) and the second pod stays
`Pending` on `Multi-Attach error for volume`. On kind you get a different and
worse outcome — two Ollama processes writing to one model directory and
competing for the same CPU and memory, which is why the phase adds capacity the
other way, with a separate Deployment and its own PVC:

```sh
kubectl -n openmodel-inference scale deploy/ollama --replicas=1
kubectl -n openmodel-inference get deploy
# ollama        1/1   qwen2.5:0.5b + nomic-embed-text, PVC ollama-models
# ollama-coder  1/1   qwen2.5-coder:0.5b,              PVC ollama-coder-models
```

`kubernetes/base/api/models.yaml` routes per model name, so a new backend is a
Deployment plus one URL. The other way out — `ReadWriteMany` storage shared by
replicas — needs a provisioner kind does not have.

## OOM drill

Deferred from Phase 3. Give the api container a limit it cannot live in:

```sh
kubectl -n openmodel-api set resources deploy/api -c=api --limits=memory=64Mi --requests=memory=64Mi
```

`-c=api` matters: without it the initContainer gets the same limit and the API
server rejects the patch, because its 128Mi request would exceed the new limit.
The HPA owns `replicas`, but this is a template change, so the Deployment
creates a *new* ReplicaSet and rolls one pod at a time. The old pods keep
serving:

```
NAME                   READY   STATUS      RESTARTS   AGE
api-5d99b4d46d-2zbvh   1/1     Running     5          34m
api-5d99b4d46d-x5gbv   1/1     Running     6          27m
api-65c97d55c4-bzjfj   0/1     OOMKilled   0          38s

kubectl -n openmodel-api describe pod api-65c97d55c4-bzjfj
    State:          Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Sun, 13 Sep 2026 12:56:23 +0200
      Finished:     Sun, 13 Sep 2026 12:56:34 +0200
    Last State:     Terminated
      Reason:       OOMKilled
```

Exit 137 is SIGKILL from the kernel's OOM killer — the process is not asked to
stop, it is shot. Python plus FastAPI plus the SQLAlchemy engine does not fit in
64Mi; 128Mi request / 512Mi limit is what the base sets.

The dashboard's restart panel and Prometheus agree:

```
increase(kube_pod_container_status_restarts_total{namespace="openmodel-api"}[15m])
{pod="api-65c97d55c4-bzjfj", container="api"}   3.03
{pod="api-5d99b4d46d-2zbvh", container="api"}   3.07   # memory pressure, not this drill
{pod="api-5d99b4d46d-x5gbv", container="api"}   4.09
```

and the rule fires:

```
curl -s localhost:9090/api/v1/alerts | jq '.data.alerts[] | select(.labels.alertname=="ApiPodRestarts")'

ApiPodRestarts   firing    api-5d99b4d46d-2zbvh
ApiPodRestarts   firing    api-5d99b4d46d-x5gbv
ApiPodRestarts   pending   api-65c97d55c4-bzjfj
```

`pending` for the pod that just started crashing (the rule's `for: 1m` has not
elapsed), `firing` for the two that had been restarting under memory pressure
for longer. Alertmanager is disabled on this cluster, so "firing" means visible
in Prometheus and Grafana and nowhere else.

Undo, and the old ReplicaSet comes back:

```sh
kubectl -n openmodel-api rollout undo deploy/api
kubectl -n openmodel-api get deploy api -o jsonpath='{.spec.template.spec.containers[0].resources}'
# {"limits":{"cpu":"500m","memory":"512Mi"},"requests":{"cpu":"100m","memory":"128Mi"}}
curl -sk -o /dev/null -w '%{http_code}\n' https://api.openmodel.test/ready   # 200
```

`rollout undo` warns that the deployment was last managed by `kubectl apply`; a
later `kubectl apply -k kubernetes/overlays/dev` puts the annotation right
again.

## Alert drill

```sh
kubectl -n openmodel-data delete pod postgres-0
```

Expected `PostgresDown` to fire. It did not:

```
12:57:32 alert=[none]     pod=[2/2 Terminating]
12:57:48 alert=[pending]  pod=[2/2 Running]
12:58:03 alert=[pending]  pod=[2/2 Running]
12:58:18 alert=[none]     pod=[2/2 Running]
```

The rule has `for: 1m` and the pod came back in about forty seconds, so the
alert reached `pending` and resolved without ever firing. That is the `for`
clause doing its job: a restart that fast is not worth waking anyone. To see the
full lifecycle you have to hold Postgres down:

```sh
kubectl -n openmodel-data scale sts postgres --replicas=0
# wait, then
kubectl -n openmodel-data scale sts postgres --replicas=1
```

```
13:08:45 postgres gone        PostgresDown=[]
13:09:26 PostgresDown=[]
13:09:46 PostgresDown=[pending]      # absent(pg_up) took ~60s to become true
13:10:46 PostgresDown=[firing]       # + the 1m `for`
13:11:26 PostgresDown=[firing]
13:11:47 scaled back to 1     PostgresDown=[firing]  pod=[0/2 Pending]
13:12:07 PostgresDown=[firing]       pod=[2/2 Running]
13:12:28 PostgresDown=[]             # resolved
```

Two delays stack up: service discovery has to notice the target is gone before
`absent(pg_up)` is true, and only then does the `for: 1m` start. From "Postgres
is gone" to "an alert fires" was two minutes, and from "Postgres is back" to
"resolved" about forty seconds. Know that number before you promise anyone an
SLO.

Also note which arm of the rule fired. `pg_up == 0` covers "the exporter is up
and cannot reach Postgres"; deleting the pod takes the exporter with it, so the
series disappears entirely and only `absent(pg_up)` catches it.

After every drill, put the cluster back and check it:

```sh
curl -sk -o /dev/null -w '%{http_code}\n' https://api.openmodel.test/ready   # 200
kubectl get hpa -n openmodel-api                                            # REPLICAS 2
kubectl diff -k kubernetes/overlays/dev
```

The diff is clean when the only objects in it are the three Jobs — `api-migrate`,
`ollama-pull`, `ollama-coder-pull` — which are reaped after they succeed and so
always read as missing. Anything else is a drill you forgot to undo.

## What to notice

**Scaling the API is not scaling the bottleneck.** The HPA moved 2 → 5 pods and
completion tokens per second did not move. Every one of those pods was doing the
same thing: holding a socket open to Ollama and forwarding bytes. Autoscaling a
proxy in front of a saturated backend adds queues, not capacity.

**Where the queue lives.** There are three, and they fail differently. Ollama's
own queue (`OLLAMA_NUM_PARALLEL` slots, everything else waits) shows up as TTFT
climbing while tokens/s stays flat. The org's concurrency cap in Redis shows up
as instant `429`s — a queue that refuses to queue, which is the kinder failure.
The kernel's run queue shows up as everything slowing down at once, including
the control plane. Read them in that order: tokens/s flat and TTFT rising is the
model; fast 429s are the plan; load average 89 is the box.

**What the HPA can and cannot see.** It scales on `llm_inflight`, which counts
requests *the api pods are holding*. It cannot see how long each one has been
waiting, so a backend that gets twice as slow looks exactly like a client that
sent twice as many requests, and the answer in both cases is "more api pods".
TTFT p95 is the metric that tells them apart, and no HPA reads it. That is why
the dashboard puts in-flight and TTFT on the same row.

**metrics-server and custom metrics are different pipelines.** metrics-server
feeds `kubectl top` and CPU/memory HPAs from the kubelet's summary API;
prometheus-adapter feeds `custom.metrics.k8s.io` from Prometheus. They share no
code and no failure mode. Under memory pressure metrics-server crashlooped 60
times while `llm_inflight` kept being served — `kubectl top` went blank and the
HPA kept scaling.

**RWO is per node, not per pod.** On a single-node kind cluster two pods happily
share one ReadWriteOnce PVC. The scheduler is what enforces the constraint, and
with one node there is nothing to enforce. Capacity for a second model comes
from a second Deployment with its own PVC, which is also the only shape that
lets you give each model different resources.

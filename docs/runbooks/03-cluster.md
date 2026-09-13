# Runbook 03 — Cluster

The same gateway, now on Kubernetes: a one-node kind cluster with Calico for
network policy, Envoy Gateway at the edge on HTTPS, Postgres and Redis as
StatefulSets, Ollama on a PVC, and the API as a Deployment of two.

Compose is not involved. Stop it first so nothing fights for ports or memory:

```sh
docker compose down
```

## Bring it up

Three steps, in order, from the repo root:

```sh
scripts/cluster-up.sh   # kind + Calico + Envoy Gateway + cert-manager
scripts/secrets.sh      # writes the git-ignored kubernetes/overlays/dev/secrets/*.env
scripts/deploy.sh       # builds the image, kind-loads it, applies the dev overlay
```

`cluster-up.sh` is idempotent: if the cluster exists it skips the create and
prints the running node image so you can spot drift from
`kubernetes/kind/cluster.yaml`. `secrets.sh` never overwrites existing files —
the passwords are baked into the Postgres data directory, so regenerating them
without wiping the PVC would lock the app out. Use `--force` only after a
`cluster-down.sh`.

One thing the scripts cannot do for you, because it needs sudo:

```
127.0.0.1 api.openmodel.test
```

Add that line to `/etc/hosts`. The kind node publishes 30080/30443 on the host
as 80/443, so once the name resolves, `https://api.openmodel.test` reaches
Envoy. The certificate is self-signed by an in-cluster cert-manager issuer, so
every curl below carries `-k`.

Check it:

```sh
curl -k https://api.openmodel.test/ready
```

Give Docker Desktop 12 GB if you can. The cluster itself sits around 2.5-3 GB,
but Ollama needs roughly 500 MB free *at request time* to load qwen2.5:0.5b.
When the VM is full — another project's containers, say — `/ready` stays 200
and embeddings keep working while chat returns
`502 "model requires more system memory"`. That is a host memory problem, not a
cluster one; `docker stats --no-stream` tells you who took it.

Then mint a key and run the acceptance suite through the gateway. The admin
token is in `kubernetes/overlays/dev/secrets/api.env` — read it into a variable
rather than pasting it anywhere:

```sh
ADMIN_TOKEN=$(grep '^OPENMODEL_ADMIN_TOKEN=' kubernetes/overlays/dev/secrets/api.env | cut -d= -f2)

cd api && OPENMODEL_BASE_URL=https://api.openmodel.test/v1 \
  OPENMODEL_INSECURE_TLS=1 \
  OPENMODEL_ADMIN_TOKEN="$ADMIN_TOKEN" \
  uv run pytest -m e2e -q
```

`OPENMODEL_INSECURE_TLS=1` is what makes the suite accept the self-signed
certificate; without it every request fails the handshake. The suite
bootstraps its own org, user and key through the admin API first, exactly the
way an operator would.

## Look around

Everything lives in five namespaces: `openmodel-edge` (the Gateway),
`openmodel-api`, `openmodel-inference`, `openmodel-data`, and `openmodel-obs`
(empty until phase 4).

```sh
kubectl get pods -A                      # the whole cluster at a glance
kubectl get all -n openmodel-api         # one tier
kubectl -n openmodel-api describe pod -l app.kubernetes.io/component=api
kubectl -n openmodel-api logs -l app.kubernetes.io/component=api --tail=20 -f
kubectl -n openmodel-api get events --sort-by=.lastTimestamp | tail -20
kubectl -n openmodel-inference exec deploy/ollama -- ollama list
```

Four things are worth knowing where to find:

- **Endpoints.** `kubectl -n openmodel-api get endpointslices -l kubernetes.io/service-name=api -o yaml`
  tells you which pod IPs the Service is actually sending traffic to, and
  whether each is `ready`. This is the single most useful thing to look at when
  the gateway 503s.
- **The Gateway's address.** `kubectl -n openmodel-edge get gateway openmodel`
  shows `PROGRAMMED=True` and the Service Envoy is listening on.
- **The routes.** `kubectl get httproute -A` shows which
  hostnames and paths attach to the Gateway, and whether they were accepted.
- **The migrate Job.** `kubectl -n openmodel-api get job api-migrate` and
  `kubectl -n openmodel-api logs job/api-migrate`. It is deleted and re-created
  on every `deploy.sh`, and reaped 300s after it succeeds, so "not found" is
  normal on a settled cluster.

## Data path

Postgres holds the accounts, Redis holds the rate-limit counters. Both are
reachable with `kubectl exec`; neither is published outside the cluster.

```sh
# tables
kubectl -n openmodel-data exec postgres-0 -- \
  psql -U postgres -d openmodel -c '\dt'

# how many keys exist, and what the last requests cost
kubectl -n openmodel-data exec postgres-0 -- \
  psql -U postgres -d openmodel -tAc 'select count(*) from api_key'
kubectl -n openmodel-data exec postgres-0 -- \
  psql -U postgres -d openmodel -c \
  'select model_name, endpoint, prompt_tokens, completion_tokens, status_code from request order by created_at desc limit 5'

# the live rate-limit and readiness keys
kubectl -n openmodel-data exec redis-0 -- redis-cli keys '*'
kubectl -n openmodel-data exec redis-0 -- redis-cli info keyspace
```

The app connects as `openmodel_app`, which has no DDL rights. Only the migrate
Job and the `wait-for-migrations` initContainer get the `openmodel_owner` DSN,
and it lives in its own Secret (`api-migrate-secrets`) so the serving container
never mounts it.

## Break it

Every drill below was run once against this cluster; the observation under each
is the real output, trimmed. After each one, restore and confirm
`curl -k https://api.openmodel.test/ready` is 200 before starting the next.

### 1. Delete an api pod

```sh
kubectl -n openmodel-api delete "$(kubectl -n openmodel-api get pods -l app.kubernetes.io/component=api -o name | head -1)"
kubectl -n openmodel-api rollout status deploy/api
```

Expected: the ReplicaSet notices desired (2) no longer matches actual (1) and
makes a replacement within a second. The other pod keeps serving, so the
gateway stays up.

```
pod "api-68d757f66f-d7b62" deleted
NAME                   READY   STATUS     RESTARTS   AGE
api-68d757f66f-g6tbj   1/1     Running    0          24m
api-68d757f66f-v48r8   0/1     Init:0/1   0          13s
deployment "api" successfully rolled out

/ready polled twice a second across the delete:
200 503 200 200 200 200 200 200 200 ... (37 more 200s)
```

One 503. Deleting a pod is not a graceful drain: Envoy learns the endpoint is
gone from the EndpointSlice watch, which lands a moment after the pod stops
accepting connections. A rollout (`kubectl rollout restart`) has no such gap,
because the new pod is ready before the old one is told to stop.

### 2. Delete the ollama pod

```sh
kubectl -n openmodel-inference delete pod -l app.kubernetes.io/name=ollama
# poll /ready every 2s while it comes back
kubectl -n openmodel-inference exec deploy/ollama -- ollama list
```

Expected: `/ready` 503 while the backend is missing, then 200. The models are
on a PVC, not in the pod, so nothing is re-downloaded.

```
pod "ollama-d6b88f888-lmfwn" deleted
503 200 200 200 200 ...            # back inside ~2s: the process starts fast
NAME                     READY   STATUS    RESTARTS   AGE
ollama-d6b88f888-xvp9s   1/1     Running   0          2m11s

NAME                       ID              SIZE      MODIFIED
nomic-embed-text:latest    0a109f422b47    274 MB    27 minutes ago
qwen2.5:0.5b               a8b0c5157701    397 MB    27 minutes ago
```

Note what did *not* happen: the api pods did not restart. `/health` is
process-only on purpose, so a dependency outage makes a pod unready, never
restarts it.

### 3. Delete postgres-0

Mint a key first and do not use it — an already-seen key is cached in Redis and
would be served from there, proving nothing.

```sh
kubectl -n openmodel-data exec postgres-0 -- psql -U postgres -d openmodel -tAc 'select count(*) from api_key'
kubectl -n openmodel-data delete pod postgres-0 --wait=false
curl -k https://api.openmodel.test/v1/models -H "Authorization: Bearer $UNUSED_KEY"
```

Expected: 503 `database_unavailable` for the uncached key while Postgres is
gone, and the same row count afterwards — the data is on
`data-postgres-0`, a PVC whose lifetime is the StatefulSet's, not the pod's.

```
rows before: api_key=4
pod "postgres-0" deleted

through the gateway, during the outage:
HTTP 503 {"error":{"message":"The database is unavailable; the request was not
served.","type":"server_error","code":"database_unavailable"}}

after postgres-0 is Ready again:
api_key=4
gateway /ready 200
models with the same key: HTTP 200
```

The api pods stayed Running throughout. They degrade (503 with a typed error)
rather than crash, which is why `/health` and `/ready` are different endpoints.

### 4. Scale api to 0

```sh
kubectl -n openmodel-api scale deploy/api --replicas=0
kubectl -n openmodel-api get endpoints api
curl -k https://api.openmodel.test/ready
kubectl -n openmodel-api scale deploy/api --replicas=2
```

Expected: no endpoints, and the gateway fails because it has nothing to send to.

```
NAME   ENDPOINTS   AGE
api    <none>      39m
gateway: HTTP 500          (empty body — this is Envoy, not the app)

after scaling back to 2, for a few seconds:  HTTP 500
then: 200 200 200 200 200
```

Two things to take away. The error body is empty: nothing of ours ran, Envoy
answered. And the recovery is not instant — Envoy picks the new endpoints up
from its own watch a beat after the pods report ready.

### 5. Wrong database password

Do not edit `secrets/api.env`; that would rewrite a Secret the running pods
share. Override the env on the Deployment instead — a container env var wins
over the same key coming from `envFrom`:

```sh
kubectl -n openmodel-api set env deploy/api \
  OPENMODEL_DATABASE_URL='postgresql+asyncpg://openmodel_app:wrong-password@postgres.openmodel-data:5432/openmodel' \
  OPENMODEL_DATABASE_OWNER_URL='postgresql+asyncpg://openmodel_owner:wrong-password@postgres.openmodel-data:5432/openmodel'

kubectl -n openmodel-api get pods
kubectl -n openmodel-api logs <new-pod> -c wait-for-migrations --tail=5
kubectl -n openmodel-api rollout undo deploy/api
```

Expected: the new pod never leaves `Init:0/1`, and because a rolling update
will not retire a healthy pod for an unhealthy one, the two old pods keep
serving. Nothing breaks for callers.

```
NAME                   READY   STATUS     RESTARTS   AGE
api-68d757f66f-f482p   1/1     Running    0          97s
api-68d757f66f-zrwrj   1/1     Running    0          97s
api-bddf457b9-fspl8    0/1     Init:0/1   0          46s

init logs:
waiting for migrations
waiting for migrations
waiting for migrations

deployment condition:
Deployment has minimum availability. ReplicaSet "api-bddf457b9" is progressing.
gateway /ready: 200
```

That output is from before this drill changed the manifest. At the time the
wait loop sent alembic's stderr to `/dev/null`, so the log was those three
words and nothing else, and the only way to the cause was to run the command by
hand inside the stuck pod:

```sh
kubectl -n openmodel-api exec <new-pod> -c wait-for-migrations -- alembic current
```

```
asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "openmodel_owner"
```

That was the drill's real lesson — a wait loop that hides its errors turns a
one-line failure into a guessing game, and `describe` shows only `Started` on
the initContainer, so no event ever says why it is looping. The `2>/dev/null`
is gone now: the same drill today prints that `InvalidPasswordError` into
`kubectl logs -c wait-for-migrations` every two seconds.

`kubectl rollout undo` puts the old template back and the stuck pod is deleted.

### 6. A tag that does not exist

```sh
kubectl -n openmodel-api set image deploy/api api=openmodel-api:nope
kubectl -n openmodel-api describe pod <new-pod> | grep -E 'Failed|BackOff'
kubectl -n openmodel-api rollout undo deploy/api
```

Expected: `ErrImagePull`, then `ImagePullBackOff`; old pods keep serving.

```
api-cb6754f58-gl6cd    0/1     ErrImagePull   0          76s

Warning  Failed   30s (x3 over 68s)  kubelet  Failed to pull image "openmodel-api:nope":
  failed to resolve reference "docker.io/library/openmodel-api:nope": pull access denied,
  repository does not exist or may require authorization
Normal   BackOff  3s (x3 over 44s)   kubelet  Back-off pulling image "openmodel-api:nope"
```

Read that message carefully: `docker.io/library/...`. An unqualified image name
is a Docker Hub name. `openmodel-api:dev` only ever works because `kind load`
puts it in the node's own image store and the manifests say
`imagePullPolicy: IfNotPresent` — one typo in the tag and the node goes looking
on the internet for an image that was never pushed anywhere.

### 7. A 64Mi memory limit

```sh
kubectl -n openmodel-api set resources deploy/api --limits=memory=64Mi
```

The first attempt is rejected before anything is created — the API server
validates the pod template, and the manifest's 128Mi *request* is now above the
limit:

```
error: failed to patch resources update to pod template Deployment.apps "api" is invalid:
spec.template.spec.containers[0].resources.requests: Invalid value: "128Mi":
must be less than or equal to memory limit of 64Mi
```

Lower both and it is accepted:

```sh
kubectl -n openmodel-api set resources deploy/api --limits=memory=64Mi --requests=memory=64Mi
kubectl -n openmodel-api get pods
kubectl -n openmodel-api describe pod <new-pod> | grep -iE 'OOM|Reason'
kubectl -n openmodel-api rollout undo deploy/api
```

Expected: the interpreter cannot even import the app in 64Mi, so the container
is killed on startup and back-off takes over.

```
api-5d656f976f-qhjnb   0/1   CrashLoopBackOff   2 (3s ago)   49s

api: restarts=2 last={"terminated":{"exitCode":137,"reason":"OOMKilled", ...}}
Warning  BackOff  2s (x4 over 28s)  kubelet  Back-off restarting failed container api
```

Exit code 137 is 128+9: the kernel's OOM killer sent SIGKILL. The pod is not
"crashed" in any way the app could have handled — there is no log line to find,
which is why `lastState.terminated.reason` is the field to check.

### 8. Break the readiness path

```sh
kubectl -n openmodel-api set env deploy/api OPENMODEL_REDIS_URL=redis://nowhere.openmodel-data:6379/0
```

As in drill 5, a rolling update alone changes nothing for callers — the healthy
old pods are never retired. To see the failure mode itself, force every pod
onto the broken template:

```sh
kubectl -n openmodel-api scale deploy/api --replicas=0
kubectl -n openmodel-api scale deploy/api --replicas=2
kubectl -n openmodel-api get endpointslices -l kubernetes.io/service-name=api \
  -o jsonpath='{range .items[*].endpoints[*]}{.addresses} ready={.conditions.ready}{"\n"}{end}'
kubectl -n openmodel-api rollout undo deploy/api
```

Expected: pods Running but 0/1, every endpoint `ready=false`, gateway down.

```
api-56fb7ff7bc-4ks4f   0/1   Running   0   52s
api-56fb7ff7bc-t9zs5   0/1   Running   0   52s

["192.168.120.56"] ready=false
["192.168.120.55"] ready=false

Warning  Unhealthy  2s (x8 over 37s)  kubelet  Readiness probe failed: HTTP probe failed with statuscode: 503
gateway: HTTP 500
```

`Running` is not `Ready`. To find out *why* it is unready, ask the pod
directly — the probe only reports a status code, but the endpoint itself
explains:

```sh
kubectl -n openmodel-api exec <pod> -c api -- \
  python -c "import httpx;r=httpx.get('http://127.0.0.1:8000/ready');print(r.status_code,r.text)"
```

```
503 {"status":"not_ready","backends":{"http://ollama.openmodel-inference:11434":"ok"},
     "redis":"ConnectionError: Error -2 connecting to nowhere.openmodel-data:6379.
      Name or service not known."}
```

Ollama fine, Redis unresolvable. That body is the reason `/ready` reports each
dependency separately instead of a bare boolean.

### 9. NetworkPolicy: nothing else may reach the api

```sh
kubectl run np-probe --image=busybox:1.36 --restart=Never --rm -i -- \
  sh -c 'wget -q -T 8 -O- http://api.openmodel-api:8000/health; echo "wget exit=$?"'
```

Expected: a timeout, not a refusal. A dropped packet has no one to send a RST.

```
wget: download timed out
wget exit=1
```

The api namespace has a `default-deny` policy plus `allow-gateway-ingress`,
which permits port 8000 only from pods in `envoy-gateway-system` labelled
`gateway.envoyproxy.io/owning-gateway-name: openmodel`. Nothing else is on the
list — including the api pods themselves, so one api pod cannot call its own
Service either. The proof it is a policy and not a broken Service is that the
same request through Envoy is 200 the whole time.

### 10. PSA: no root pods in this namespace

```sh
kubectl -n openmodel-api run root-test --image=busybox --restart=Never \
  --overrides='{"spec":{"securityContext":{"runAsUser":0}}}'
```

Expected: rejected at admission. No pod object is ever created.

```
Error from server (Forbidden): pods "root-test" is forbidden: violates PodSecurity
"restricted:latest": allowPrivilegeEscalation != false (container "root-test" must set
securityContext.allowPrivilegeEscalation=false), unrestricted capabilities (container
"root-test" must set securityContext.capabilities.drop=["ALL"]), runAsNonRoot != true,
runAsUser=0 (pod must not set runAsUser=0), seccompProfile (pod or container "root-test"
must set securityContext.seccompProfile.type to "RuntimeDefault" or "Localhost")
```

The namespace carries `pod-security.kubernetes.io/enforce: restricted`. Note it
lists *all* five violations at once, not just the first: fixing them one at a
time is unnecessary.

### 11. Rebuild the whole thing

The real test of the bootstrap: delete the cluster and build it again from the
same three scripts.

```sh
time (scripts/cluster-down.sh && scripts/cluster-up.sh && scripts/secrets.sh && scripts/deploy.sh)
```

This wipes every PVC, so Ollama re-downloads ~700 MB of weights. The
`/etc/hosts` line survives — it is on your machine, not in the cluster — and so
do `secrets/*.env`, which is why the admin token from before still works.

```
down=4s   cluster-up=211s   scripts total=419s
```

Seven minutes of scripts — and then a cluster that did not work. Two things
went wrong, both worth knowing about.

**The migrate Job outran the images.** On a cold node every image is pulled
from scratch, and `postgres:18` took over five minutes. The migrate Job started
immediately, could not resolve or reach a Postgres that did not exist yet, and
burned its `backoffLimit: 3` long before the database was up:

```
openmodel-api   api-migrate-bbk7n   0/1   Error      3m19s
openmodel-api   api-migrate-qw6tx   0/1   Error      3m4s
openmodel-api   api-migrate-hnlhq   0/1   Error      2m39s
openmodel-api   api-migrate-jmqqt   0/1   Error      114s
openmodel-data  postgres-0          0/1   ContainerCreating   3m19s

job log: socket.gaierror: [Errno -2] Name or service not known
postgres-0 events: Pulling image "postgres:18"   (still, 3m26s in)
```

`deploy.sh` then failed its own `wait --for=condition=complete job/api-migrate
--timeout=180s`. The fix is simply to run `scripts/deploy.sh` again once the
images are down — it deletes and re-creates the Job, which is why that step
exists. On the second run migrations completed and the api pods went Ready.
Worth remembering: on a cold cluster, expect one failed `deploy.sh` and re-run
it.

**The model pull could never have worked.** The `ollama-pull` Job kept failing
with `BackoffLimitExceeded`, and its log said:

```
Error: pull model manifest: Get "https://registry.ollama.ai/v2/library/qwen2.5/manifests/0.5b":
  dial tcp 104.18.17.170:443: i/o timeout
```

The Job had the NetworkPolicy hole to the registry, so that looks impossible
until you read the server log:

```sh
kubectl -n openmodel-inference logs deploy/ollama | grep -i 'request failed'
```

```
level=INFO source=images.go:714 msg="request failed: Get \"https://registry.ollama.ai/...\":
  dial tcp 104.18.17.170:443: i/o timeout"
[GIN] | 200 | 30.04s | 192.168.120.32 | POST "/api/pull"
```

`ollama pull` with `OLLAMA_HOST` set to a URL is a *client* call. The Job asks
the Deployment to fetch the weights; the Deployment is what opens the
connection to the registry — and its egress was DNS-only. The drills before
this one never caught it, because those models had been pulled before the
NetworkPolicies were written and had lived on the PVC ever since. Only a
from-scratch rebuild could surface it. `allow-ollama-registry-egress` now puts
the hole on the server pods, where the traffic actually originates, and the
pull completes:

```
job condition: SuccessCriteriaMet Complete
NAME                       ID              SIZE      MODIFIED
nomic-embed-text:latest    0a109f422b47    274 MB    16 seconds ago
qwen2.5:0.5b               a8b0c5157701    397 MB    55 seconds ago
```

That is the argument for doing this drill at all: nothing short of deleting the
cluster proves the bootstrap, and a rule scoped to the wrong pod is invisible
for as long as the PVC survives.

## What to notice

**Desired state, not commands.** Nothing above was repaired by hand. You delete
a pod, a controller compares desired (2) with actual (1) and acts. `kubectl set
env`, `set image`, `set resources` and `scale` all edit the same thing — the
desired state — and `rollout undo` restores the previous one. The gap between
the two is what `kubectl rollout status`, `describe`, and a Deployment's
`conditions` are reporting on.

**A rolling update is a guardrail.** Drills 5, 6 and 7 were all fatal to the
new pods and none of them caused an outage: a broken template produces pods
that never become ready, and a healthy pod is never retired for one of those.
That is also why those failures are quiet. Nobody pages you; the deploy just
sits there. `kubectl rollout status` is the thing that tells you.

**Service versus pod IP.** Pod IPs change every time a pod is replaced.
Deleting one api pod, with `kubectl get pods -o wide` before and after:

```
api-54f44cb669-l25tv   192.168.120.30      ->   api-54f44cb669-2ccrr   192.168.120.35
api-54f44cb669-xps55   192.168.120.31      ->   api-54f44cb669-xps55   192.168.120.31
```

A new name, a new address, and nothing had to be reconfigured, because
everything talks to a Service DNS name (`postgres.openmodel-data`,
`redis.openmodel-data`, `ollama.openmodel-inference`). The Service is the
stable name; the EndpointSlice behind it is the live list of pod IPs, filtered
by readiness. When the gateway 503s, that list is the first thing to look at:
empty means no ready pods, and the cause is in the pods, not in the routing.

**PVC lifetime versus pod lifetime.** Drill 2 and drill 3 deleted the pod that
owned the data and lost nothing — the PVC outlives the pod. Drill 11 deleted
the cluster and lost everything, because a PVC does not outlive its
StorageClass's node. That is the whole distinction: `kubectl delete pod` is
safe, `kind delete cluster` is not.

**PSA and NetworkPolicy are guardrails of different kinds.** PSA runs at
admission: a non-conforming pod is never created, and you find out immediately
with a precise message (drill 10). NetworkPolicy runs in the data plane: the
object is created and looks fine, and you find out as a timeout much later
(drill 9). The second failure mode is much harder to debug, so when something
cannot reach something else, check policy before you suspect DNS or the app.

**Namespaces are the blast radius.** The five namespaces are not tidiness —
they are the unit everything else is scoped to. PSA labels are per namespace,
NetworkPolicies select by namespace, Secrets do not cross one, and the drills
above never touched a tier they were not aimed at: breaking the api's Redis URL
left Postgres, Ollama and Envoy alone. Whatever you break, the question worth
asking first is which namespace it is in.

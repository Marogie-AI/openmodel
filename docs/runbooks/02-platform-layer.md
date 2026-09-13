# Runbook 02 — Platform layer

Who the caller is, what their plan allows, how fast they may call, and what it
cost. Postgres holds the accounts, Redis holds the limits, and every request
now needs an API key.

Ports below assume the defaults: the gateway on 8000 and Postgres on 5432. On
this machine the stack is published elsewhere, so start it and read the rest
with 8010 and 55433 in place of those:

```sh
OPENMODEL_HOST_PORT=8010 OPENMODEL_PG_HOST_PORT=55433 docker compose up -d --build
```

`migrate` is a one-shot service that runs `alembic upgrade head` and exits 0
before `api` starts. If it exits non-zero, `api` never starts — read its log:

```sh
docker compose logs migrate
```

## Bootstrap

The stack ships with two plans and no accounts. Admin endpoints are gated by a
shared token, not by a key:

```sh
export ADMIN='X-Admin-Token: dev-admin-token-change-me'
export JSON='content-type: application/json'
```

Change that token in any environment you care about (`OPENMODEL_ADMIN_TOKEN`).

See what the plans allow:

```sh
curl -s localhost:8000/admin/plans -H "$ADMIN"
```

`free` is 60 requests per minute, 2 concurrent, `qwen2.5:0.5b` and
`nomic-embed-text`. `pro` is 600 per minute, 10 concurrent, and adds
`qwen2.5-coder:0.5b`.

Create an organization, a user in it, and a key for that user:

```sh
curl -s -X POST localhost:8000/admin/orgs -H "$ADMIN" -H "$JSON" \
  -d '{"name":"acme","plan_code":"free"}'

curl -s -X POST localhost:8000/admin/users -H "$ADMIN" -H "$JSON" \
  -d '{"organization_id":"<org id from above>","email":"dev@acme.test"}'

curl -s -X POST localhost:8000/admin/users/<user id>/keys -H "$ADMIN"
```

The last call is the only time the raw key is ever readable:

```json
{"id":"...","key":"om_dev_NtpXGuKoSCfP_...","prefix":"om_dev_NtpXGuKoSCfP","created_at":"..."}
```

Only the prefix and an argon2id hash of the secret are stored. Lose the key and
you mint a new one; there is no recovery path, by design.

```sh
export KEY=om_dev_...
```

## Use a key

```sh
curl -s localhost:8000/v1/models -H "Authorization: Bearer $KEY"

curl -s localhost:8000/v1/chat/completions -H "Authorization: Bearer $KEY" -H "$JSON" \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Say hi."}]}'
```

`/v1/models` lists the intersection of what this deployment serves and what the
caller's plan includes, so a `free` key sees two models and a `pro` key sees
three. Asking for a model the plan does not include is a 403, whether or not
the deployment serves it:

```sh
curl -s localhost:8000/v1/chat/completions -H "Authorization: Bearer $KEY" -H "$JSON" \
  -d '{"model":"qwen2.5-coder:0.5b","messages":[{"role":"user","content":"hi"}]}'
```

```json
{"error":{"message":"Your plan does not include the model 'qwen2.5-coder:0.5b'.","type":"invalid_request_error","code":"model_not_allowed"}}
```

No key at all is a 401 in the same envelope, with `WWW-Authenticate: Bearer`.

A caller can mint and revoke their own keys without the admin token:

```sh
curl -s localhost:8000/api/keys -H "Authorization: Bearer $KEY"
curl -s -X POST localhost:8000/api/keys -H "Authorization: Bearer $KEY"
curl -s -X DELETE localhost:8000/api/keys/<key id> -H "Authorization: Bearer $KEY"
```

Revoking drops the key from the Redis cache immediately, so it stops working on
the next request rather than at the end of the cache TTL.

## Hit the limit

Limits are per organization, not per key. The bucket holds one minute's worth
of requests and refills steadily, so a burst of 60 drains a `free` org and
every request after that is refused until the bucket refills:

```sh
for i in $(seq 1 70); do
  curl -s -o /dev/null -D - -H "Authorization: Bearer $KEY" -H "$JSON" \
    -d '{"model":"nomic-embed-text","input":"hi"}' localhost:8000/v1/embeddings \
    | grep -iE 'HTTP/|retry-after|ratelimit'
done
```

Successful responses carry the budget:

```
HTTP/1.1 200 OK
x-ratelimit-limit: 60
x-ratelimit-remaining: 43
```

and the first refusal carries how long to wait:

```
HTTP/1.1 429 Too Many Requests
retry-after: 1
x-ratelimit-limit: 60
x-ratelimit-remaining: 0
```

```json
{"error":{"message":"Rate limit reached for organization: 60 requests per minute.","type":"rate_limit_error","code":"rate_limit_exceeded"}}
```

Concurrency is a separate cap: `free` allows 2 requests in flight. Start two
slow streams and try a third:

```sh
BODY='{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Write a long story."}],"max_tokens":400,"stream":true}'
curl -sN -o /dev/null -H "Authorization: Bearer $KEY" -H "$JSON" -d "$BODY" localhost:8000/v1/chat/completions &
curl -sN -o /dev/null -H "Authorization: Bearer $KEY" -H "$JSON" -d "$BODY" localhost:8000/v1/chat/completions &
sleep 2
curl -s -H "Authorization: Bearer $KEY" -H "$JSON" \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"hi"}]}' \
  localhost:8000/v1/chat/completions
wait
```

```json
{"error":{"message":"Too many concurrent requests","type":"rate_limit_error","code":"rate_limit_exceeded"}}
```

The slot is released when the response finishes, streams included, so the third
request succeeds as soon as one of the streams ends.

## Read usage

Every served request writes a row after the response has gone out. Totals for
the caller's own organization over the last 30 days:

```sh
curl -s localhost:8000/api/usage -H "Authorization: Bearer $KEY"
curl -s 'localhost:8000/api/usage?group_by=model' -H "Authorization: Bearer $KEY"
curl -s 'localhost:8000/api/usage?group_by=user&from=2026-09-01T00:00:00Z' -H "Authorization: Bearer $KEY"
```

```json
{
  "organization_id": "6bdc92a6-...",
  "from": "2026-08-12T11:50:37.795623Z",
  "to": "2026-09-11T11:50:37.795623Z",
  "requests": 75,
  "prompt_tokens": 295,
  "completion_tokens": 767,
  "groups": [
    {"key": "nomic-embed-text", "requests": 73, "prompt_tokens": 219, "completion_tokens": 0},
    {"key": "qwen2.5:0.5b", "requests": 2, "prompt_tokens": 76, "completion_tokens": 767}
  ]
}
```

`group_by` accepts `model`, `user` and `day`. A key only ever sees its own
organization; there is no parameter for asking about someone else's.

## Inspect

Postgres, as the owner role:

```sh
docker compose exec postgres psql -U openmodel_owner -d openmodel
```

Nothing is duplicated across tables — the plan's limits live only on `plan`,
the plan a customer is on lives only on `organization`, and a request row
carries only the key that made it. Everything else is a join:

```sql
select o.name as org, o.plan_code, u.email, k.prefix,
       r.model_name, r.status_code, r.prompt_tokens, r.latency_ms, r.created_at
from request r
join api_key k      on k.id = r.api_key_id
join app_user u     on u.id = k.user_id
join organization o on o.id = u.organization_id
order by r.created_at desc
limit 20;
```

```
 org  | plan_code |       email       |       prefix        |  model_name  | status_code | prompt_tokens | latency_ms
------+-----------+-------------------+---------------------+--------------+-------------+---------------+-----------
 tiny | free      | tiny@example.test | om_dev_NtpXGuKoSCfP | qwen2.5:0.5b |         200 |            38 |      11523
```

Move `acme` to `pro` by editing one column and every key on it changes limits at
once:

```sql
update organization set plan_code = 'pro' where name = 'acme';
```

From the host instead of inside the container, on this machine:

```sh
psql postgresql://openmodel_owner:owner@localhost:55433/openmodel
```

Redis, which holds only derived state:

```sh
docker compose exec redis redis-cli --scan --pattern 'auth:*'       # resolved principals
docker compose exec redis redis-cli --scan --pattern 'rl:*'         # token buckets, per org
docker compose exec redis redis-cli --scan --pattern 'cc:*'         # in-flight counts, per org
docker compose exec redis redis-cli --scan --pattern 'plan_keys:*'  # which keys to drop on a plan edit
docker compose exec redis redis-cli ttl auth:NtpXGuKoSCfP
docker compose exec redis redis-cli hgetall rl:<org id>
```

`auth:*` is keyed by the key id — the readable middle segment of the raw key,
never the secret. Entries expire after 5 minutes. `FLUSHDB` costs one argon2
verification per key and nothing else; there is no data in Redis worth backing
up.

## Break it

Both dependencies fail closed. A rate limiter that is down would otherwise mean
unlimited traffic, and a database that is down would mean unauthenticated
traffic — a 503 is the smaller problem.

Stop Redis:

```sh
docker compose stop redis
```

Every authenticated request is refused, including `/v1/models`, because
resolving the caller reads the cache before anything else:

```json
{"error":{"message":"Rate limiting is unavailable; the request was not served.","type":"server_error","code":"rate_limiter_unavailable"}}
```

`/health` stays 200 throughout — the process is fine, its dependency is not.
`docker compose start redis` and the next request is served. Each bucket key is
absent and so starts full, which briefly hands every caller a fresh minute's
budget.

Stop Postgres:

```sh
docker compose stop postgres
```

A key that has been used in the last 5 minutes keeps working, chat included:
its principal is in the Redis cache and the cache-hit path issues no query. A
key that is not cached, and anything that reads the database directly
(`/api/usage`, every `/admin` endpoint), is refused:

```json
{"error":{"message":"The database is unavailable; the request was not served.","type":"server_error","code":"database_unavailable"}}
```

Usage metering is the one exception: it is fire-and-forget, so those rows are
lost rather than failing the request that produced them. Watch for
`usage_write_failed` in the logs.

`docker compose start postgres` and everything recovers on the next request —
the pool reconnects on its own, no restart needed.

## What to notice

- The raw key exists for exactly one HTTP response. Everything afterwards works
  from the key id and an argon2id hash, so a database dump leaks no usable key.
- Redis is a cache and a counter, never a source of truth. Postgres knows who
  everyone is; Redis only remembers, for five minutes, what Postgres said.
- Limits attach to the organization. Handing a team five keys buys them five
  ways to spend one budget, not five budgets.
- Changing a plan takes effect at once, not after a TTL: `PUT /admin/plans/free`
  drops exactly the cached principals on that plan, which is what
  `plan_keys:free` is for.
- Failing closed is a choice with a cost. The trade is stated plainly: during a
  Redis outage this gateway serves nobody rather than serving everybody for
  free.

#!/usr/bin/env bash
# Build the API image, side-load it into kind, and apply the dev overlay.
# Idempotent: safe to re-run after any edit to api/ or kubernetes/.
set -euo pipefail

CLUSTER=openmodel
IMAGE=openmodel-api:dev
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> checking kubernetes/base/data is in sync with api/sql"
"$REPO_ROOT/scripts/sync-sql.sh" --check

echo "==> building $IMAGE"
docker build -t "$IMAGE" "$REPO_ROOT/api"

echo "==> loading $IMAGE into kind cluster '$CLUSTER'"
kind load docker-image "$IMAGE" --name "$CLUSTER"

# A Job's spec is immutable, so the completed one has to go before apply or the
# new image never runs its migrations.
echo "==> deleting any previous migrate job"
kubectl -n openmodel-api delete job api-migrate --ignore-not-found

echo "==> applying kubernetes/overlays/dev"
kubectl apply -k "$REPO_ROOT/kubernetes/overlays/dev"

echo "==> waiting for migrations"
kubectl -n openmodel-api wait --for=condition=complete job/api-migrate --timeout=180s

# The tag never changes, so IfNotPresent leaves running pods on the old layers
# even after `kind load`. Restarting unconditionally is the cheap, correct fix.
echo "==> restarting api to pick up the freshly loaded image"
kubectl -n openmodel-api rollout restart deploy/api
kubectl -n openmodel-api rollout status deploy/api --timeout=180s

# May already have been reaped by its TTL, in which case there is nothing to
# wait for and the models are long since on the PVC. If it is there, a failed
# pull has to fail the deploy — the API is useless without weights.
if kubectl -n openmodel-inference get job ollama-pull >/dev/null 2>&1; then
  echo "==> waiting for the model pull job"
  kubectl -n openmodel-inference wait --for=condition=complete job/ollama-pull --timeout=600s
else
  echo "==> model pull job already reaped by its TTL; skipping"
fi

echo "==> cluster state"
kubectl get pods -A

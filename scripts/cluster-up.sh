#!/usr/bin/env bash
set -euo pipefail

CLUSTER=openmodel
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CALICO_VERSION=v3.30.0
GATEWAY_API_VERSION=v1.3.0
ENVOY_GATEWAY_VERSION=v1.4.0
CERT_MANAGER_VERSION=v1.17.2

if kind get clusters | grep -qx "$CLUSTER"; then
  echo "==> kind cluster '$CLUSTER' already exists, skipping create"
else
  echo "==> creating kind cluster '$CLUSTER'"
  kind create cluster --config "$REPO_ROOT/kubernetes/kind/cluster.yaml"
fi
kubectl config use-context "kind-$CLUSTER"

echo "==> installing Calico ($CALICO_VERSION) via Tigera operator"
# v3.30 ships the operator CRDs in a separate manifest; both need server-side apply (large CRDs).
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/operator-crds.yaml"
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"
kubectl wait --for=condition=Available deployment/tigera-operator -n tigera-operator --timeout=300s
kubectl apply -f "$REPO_ROOT/kubernetes/kind/calico-installation.yaml"

echo "==> waiting for calico-node"
until kubectl get pods -A -l k8s-app=calico-node 2>/dev/null | grep -q calico-node; do sleep 5; done
kubectl wait --for=condition=Ready pods -A -l k8s-app=calico-node --timeout=300s
kubectl wait --for=condition=Ready nodes --all --timeout=300s

# Gateway API CRDs ship inside the gateway-helm chart (bundle v1.3.0, experimental channel).
# Applying standard-install.yaml separately fights the chart for field ownership, so let helm own them.
echo "==> installing Envoy Gateway ($ENVOY_GATEWAY_VERSION) + Gateway API CRDs ($GATEWAY_API_VERSION)"
helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm \
  --version "$ENVOY_GATEWAY_VERSION" -n envoy-gateway-system --create-namespace --wait --timeout 10m
kubectl wait --for=condition=Available deployment/envoy-gateway -n envoy-gateway-system --timeout=300s

echo "==> installing cert-manager ($CERT_MANAGER_VERSION)"
helm repo add jetstack https://charts.jetstack.io --force-update
helm repo update jetstack
helm upgrade --install cert-manager jetstack/cert-manager \
  -n cert-manager --create-namespace --version "$CERT_MANAGER_VERSION" \
  --set crds.enabled=true --wait --timeout 10m
kubectl wait --for=condition=Available deployment/cert-manager -n cert-manager --timeout=300s

cat <<'MSG'

==> cluster ready. Add this line to /etc/hosts (needs sudo, not done for you):

127.0.0.1 api.openmodel.test

MSG

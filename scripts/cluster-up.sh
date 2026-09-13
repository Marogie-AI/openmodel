#!/usr/bin/env bash
set -euo pipefail

CLUSTER=openmodel
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CALICO_VERSION=v3.30.0
# Gateway API CRDs are not installed separately: gateway-helm bundles them
# (bundle v1.3.0, experimental channel). Re-check that bundle when bumping the chart.
ENVOY_GATEWAY_VERSION=v1.4.0
CERT_MANAGER_VERSION=v1.17.2
KUBE_PROMETHEUS_STACK_VERSION=77.5.0
METRICS_SERVER_VERSION=3.13.0
PROMETHEUS_ADAPTER_VERSION=4.14.1

if kind get clusters | grep -qx "$CLUSTER"; then
  echo "==> kind cluster '$CLUSTER' already exists, skipping create"
  echo "    running node image/version (drift check vs kubernetes/kind/cluster.yaml):"
  kubectl --context "kind-$CLUSTER" get nodes \
    -o custom-columns=NODE:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion,IMAGE:.status.nodeInfo.osImage
else
  echo "==> creating kind cluster '$CLUSTER'"
  kind create cluster --config "$REPO_ROOT/kubernetes/kind/cluster.yaml"
fi
kubectl config use-context "kind-$CLUSTER"
echo "==> current kubectl context is now kind-$CLUSTER"

echo "==> installing Calico ($CALICO_VERSION) via Tigera operator"
# v3.30 ships the operator CRDs in a separate manifest; both need server-side apply (large CRDs).
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/operator-crds.yaml"
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"
kubectl wait --for=condition=Available deployment/tigera-operator -n tigera-operator --timeout=300s
kubectl apply -f "$REPO_ROOT/kubernetes/kind/calico-installation.yaml"

echo "==> waiting for calico-node"
for _ in $(seq 60); do
  kubectl get pods -A -l k8s-app=calico-node 2>/dev/null | grep -q calico-node && break
  sleep 5
done
if ! kubectl get pods -A -l k8s-app=calico-node 2>/dev/null | grep -q calico-node; then
  echo "calico-node pods never appeared after 300s; check 'kubectl -n tigera-operator logs deploy/tigera-operator'" >&2
  exit 1
fi
kubectl wait --for=condition=Ready pods -A -l k8s-app=calico-node --timeout=300s
kubectl wait --for=condition=Ready nodes --all --timeout=300s

# Gateway API CRDs ship inside the gateway-helm chart (bundle v1.3.0, experimental channel).
# Applying standard-install.yaml separately fights the chart for field ownership, so let helm own them.
echo "==> installing Envoy Gateway ($ENVOY_GATEWAY_VERSION) + bundled Gateway API CRDs"
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

echo "==> installing the monitoring stack"
# Namespaces (with their PSA labels) must exist before helm, or chart pods land
# in an unlabelled namespace and the restricted enforcement is never exercised.
kubectl apply -f "$REPO_ROOT/kubernetes/base/namespaces.yaml"

# Grafana starts with `admin.existingSecret: grafana-admin`, so the Secret has
# to exist before helm --wait, which is before deploy.sh ever runs. The dev
# overlay generates the same Secret from the same file, so re-applying the
# overlay later is a no-op rather than a conflict.
"$REPO_ROOT/scripts/secrets.sh" >/dev/null
kubectl -n openmodel-monitoring create secret generic grafana-admin \
  --from-env-file="$REPO_ROOT/kubernetes/overlays/dev/secrets/grafana.env" \
  --dry-run=client -o yaml | kubectl apply -f -

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update
helm repo update prometheus-community metrics-server

helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  --version "$KUBE_PROMETHEUS_STACK_VERSION" -n openmodel-monitoring \
  -f "$REPO_ROOT/monitoring/kube-prometheus-stack.values.yaml" --wait --timeout 10m

helm upgrade --install metrics-server metrics-server/metrics-server \
  --version "$METRICS_SERVER_VERSION" -n kube-system \
  -f "$REPO_ROOT/monitoring/metrics-server.values.yaml" --wait --timeout 5m

# prometheus-adapter serves custom.metrics.k8s.io (llm_inflight) for the api
# HPA. It goes after kps because it needs the Prometheus Service to exist to
# pass its own readiness probe under --wait.
helm upgrade --install prometheus-adapter prometheus-community/prometheus-adapter \
  --version "$PROMETHEUS_ADAPTER_VERSION" -n openmodel-monitoring \
  -f "$REPO_ROOT/monitoring/prometheus-adapter.values.yaml" --wait --timeout 5m

# Two policy files hardcode this environment's addressing: inference.yaml carves
# the node's own network out of 0.0.0.0/0 by CIDR, and monitoring.yaml allows the
# node IP for the kubelet scrape and for port-forward. Docker does not hand every
# machine the same bridge subnet, so print it rather than let a mismatch be silent.
BRIDGE_CIDR="$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' \
  2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -1)"
NODE_IP="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
echo "==> node/bridge CIDR: ${BRIDGE_CIDR:-unknown}, node IP: ${NODE_IP:-unknown}"
echo "    both are hardcoded in kubernetes/base/policies/{inference,monitoring}.yaml"

cat <<'MSG'

==> cluster ready. Add this line to /etc/hosts (needs sudo, not done for you):

127.0.0.1 api.openmodel.test

MSG

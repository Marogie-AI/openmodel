#!/usr/bin/env bash
# Kustomize refuses configMapGenerator files outside its root, so the canonical
# files under api/sql/ are copied into kubernetes/base/data/ and committed.
# No args: copy. --check: fail if the copies have drifted (used by deploy.sh).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pairs=(
  "api/sql/postgresql.conf:kubernetes/base/data/postgresql.conf"
  "api/sql/init/01_roles.sh:kubernetes/base/data/01_roles.sh"
)

for pair in "${pairs[@]}"; do
  src="$root/${pair%%:*}" dst="$root/${pair##*:}"
  if [[ "${1:-}" == "--check" ]]; then
    cmp -s "$src" "$dst" || { echo "drift: $dst is stale, run scripts/sync-sql.sh" >&2; exit 1; }
  else
    cp "$src" "$dst"
  fi
done

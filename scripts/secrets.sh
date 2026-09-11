#!/usr/bin/env bash
# Generates the git-ignored env files the dev overlay's secretGenerator reads.
# Existing files are left alone unless --force (regenerating would orphan the
# passwords already baked into the postgres data dir).
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/kubernetes/overlays/dev/secrets"
force="${1:-}"
mkdir -p "$dir"

if [[ -e "$dir/postgres.env" || -e "$dir/api.env" ]] && [[ "$force" != "--force" ]]; then
  echo "secrets already exist in $dir (use --force to regenerate)"
  exit 0
fi

# hex, not base64: these end up inside DSNs where +/= would need escaping.
gen() { openssl rand -hex 24; }
owner="$(gen)" app="$(gen)"

cat > "$dir/postgres.env" <<EOT
superuser-password=$(gen)
owner-password=$owner
app-password=$app
EOT

cat > "$dir/api.env" <<EOT
OPENMODEL_KEY_PEPPER=$(gen)
OPENMODEL_ADMIN_TOKEN=$(gen)
OPENMODEL_DATABASE_URL=postgresql+asyncpg://openmodel_app:$app@postgres.openmodel-data:5432/openmodel
OPENMODEL_DATABASE_OWNER_URL=postgresql+asyncpg://openmodel_owner:$owner@postgres.openmodel-data:5432/openmodel
EOT

chmod 600 "$dir/postgres.env" "$dir/api.env"
echo "wrote $dir/postgres.env $dir/api.env"

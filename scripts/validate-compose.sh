#!/usr/bin/env bash
# Validate checked-in Compose models without reading deployment secrets or starting services.
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose)
if [[ -n "${COMPOSE_BIN:-}" ]]; then
  compose=("$COMPOSE_BIN")
fi
while IFS= read -r -d '' file; do
  echo "Validating $file"
  "${compose[@]}" -f "$file" config --no-interpolate --no-env-resolution --quiet
done < <(find dell ionos medion shared synology thinkpad -type f \( -name docker-compose.yml -o -name docker-compose.yaml \) -print0)
"${compose[@]}" -f shared/backup/docker-compose.yml -f shared/backup/compose.medion.yml config --no-interpolate --no-env-resolution --quiet

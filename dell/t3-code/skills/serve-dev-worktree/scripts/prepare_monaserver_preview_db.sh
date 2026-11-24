#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  echo "prepare_monaserver_preview_db: $*" >&2
  exit 1
}

resolve_env_file() {
  local selected=${1:-}
  local worktree
  local candidate
  local -a sibling_env_files=()

  if [[ -n "$selected" ]]; then
    [[ -f "$selected" ]] || die "missing development environment file: $selected"
    printf '%s\n' "$selected"
    return
  fi

  candidate="$repo_root/.env.dev"
  if [[ -f "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return
  fi

  # .env.dev is local workspace configuration, not branch content. For a new
  # worktree, reuse the one unambiguous sibling copy without copying secrets.
  while IFS= read -r worktree; do
    [[ -n "$worktree" && "$worktree" != "$repo_root" ]] || continue
    candidate="$worktree/.env.dev"
    [[ -f "$candidate" ]] && sibling_env_files+=("$candidate")
  done < <(git -C "$repo_root" worktree list --porcelain | sed -n 's/^worktree //p')

  case "${#sibling_env_files[@]}" in
    1)
      printf '%s\n' "${sibling_env_files[0]}"
      ;;
    0)
      die "no .env.dev found in this worktree or its siblings; pass --env-file PATH"
      ;;
    *)
      printf 'Found multiple sibling .env.dev files:\n' >&2
      printf '  %s\n' "${sibling_env_files[@]}" >&2
      die 'pass --env-file PATH to choose the intended shared development configuration'
      ;;
  esac
}

repo_root=
slug=
env_file=
runtime_dir=${XDG_RUNTIME_DIR:-/tmp}/serve-dev-worktree
dry_run=false

while (($# > 0)); do
  case "$1" in
    --repo-root) (($# >= 2)) || die '--repo-root needs a path'; repo_root=$2; shift 2 ;;
    --slug) (($# >= 2)) || die '--slug needs a value'; slug=$2; shift 2 ;;
    --env-file) (($# >= 2)) || die '--env-file needs a path'; env_file=$2; shift 2 ;;
    --runtime-dir) (($# >= 2)) || die '--runtime-dir needs a path'; runtime_dir=$2; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help)
      printf 'Usage: %s --repo-root PATH --slug SLUG [--env-file PATH] [--runtime-dir PATH] [--dry-run]\n' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$repo_root" ]] || die '--repo-root is required'
repo_root=$(cd -- "$repo_root" 2>/dev/null && pwd -P) || die 'repository root does not exist'
[[ -d "$repo_root/go-server/internal/db/migrations" ]] || die 'repository does not contain MonaServer migrations'
[[ -n "$slug" ]] || die '--slug is required'
[[ "$slug" =~ ^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$ ]] || die 'slug must be lowercase DNS-safe text, at most 48 characters'
env_file=$(resolve_env_file "$env_file")
env_file=$(cd -- "$(dirname -- "$env_file")" && printf '%s/%s\n' "$PWD" "$(basename -- "$env_file")")
[[ "$runtime_dir" == /* ]] || die '--runtime-dir must be an absolute path'

db_name="ms_preview_${slug//-/_}"
shim_file="$runtime_dir/env/$slug.sh"

if [[ "$dry_run" == true ]]; then
  printf 'PREVIEW_DATABASE_NAME=%s\nDEV_ENV_FILE=%s\n' "$db_name" "$shim_file"
  printf 'DEVELOPMENT_ENV_SOURCE=%s\n' "$env_file"
  exit 0
fi

for command_name in pg_isready psql python3; do
  command -v "$command_name" >/dev/null 2>&1 || die "required command is missing: $command_name"
done

# The T3 server runs without root so Chrome can keep its sandbox. The container
# grants only psql as the local postgres account; retain support for root hosts.
postgres_psql() {
  if (( EUID == 0 )); then
    runuser -u postgres -- psql "$@"
  else
    sudo -n -u postgres -- psql "$@"
  fi
}

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
[[ -n "${DATABASE_URL:-}" ]] || die 'DATABASE_URL is missing from the selected env file'

mapfile -t db_parts < <(python3 - <<'PY'
import os
from urllib.parse import unquote, urlsplit

url = urlsplit(os.environ['DATABASE_URL'])
if url.scheme not in {'postgres', 'postgresql'}:
    raise SystemExit('DATABASE_URL must use postgres or postgresql')
if not url.hostname or not url.path or url.path == '/':
    raise SystemExit('DATABASE_URL must include a host and database name')
print(unquote(url.username or ''))
print(url.hostname.lower())
print(url.port or 5432)
print(unquote(url.path.lstrip('/')))
PY
) || die 'could not parse DATABASE_URL'
(( ${#db_parts[@]} == 4 )) || die 'could not parse DATABASE_URL'
db_owner=${db_parts[0]}
db_host=${db_parts[1]}
db_port=${db_parts[2]}
source_db=${db_parts[3]}
[[ "$db_owner" =~ ^[A-Za-z_][A-Za-z0-9_$-]*$ ]] || die 'DATABASE_URL has an unsupported PostgreSQL username'
case "$db_host" in
  db|postgres|localhost|127.0.0.1) ;;
  *) die 'preview databases may only be derived from a local PostgreSQL endpoint' ;;
esac
case "$db_port" in
  5432|5433) ;;
  *) die 'preview databases may only use the local PostgreSQL port 5432 or 5433' ;;
esac
[[ "$source_db" =~ ^[A-Za-z0-9_-]+$ ]] || die 'DATABASE_URL has an unsupported database name'
if [[ "${source_db,,}" =~ (prod|production|stage|staging|live) ]]; then
  die 'refusing to derive a preview database from a production or staging database name'
fi

pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 || die 'PostgreSQL is not ready on 127.0.0.1:5432'
latest_migration=$(find "$repo_root/go-server/internal/db/migrations" -maxdepth 1 -type f -name '[0-9]*.up.sql' -printf '%f\n' | sort | tail -n 1 | cut -d_ -f1)
[[ "$latest_migration" =~ ^[0-9]+$ ]] || die 'could not determine the latest embedded migration'

postgres_psql -d postgres -v ON_ERROR_STOP=1 \
  -v preview_db="$db_name" -v preview_owner="$db_owner" <<'SQL' >/dev/null
SELECT format('CREATE DATABASE %I OWNER %I', :'preview_db', :'preview_owner')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'preview_db')
\gexec
SQL

actual_owner=$(postgres_psql -d postgres -Atqc \
  "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$db_name'")
[[ "$actual_owner" == "$db_owner" ]] || die "preview database owner mismatch for $db_name"

postgres_psql -q -d "$db_name" -v ON_ERROR_STOP=1 \
  -c 'CREATE EXTENSION IF NOT EXISTS postgis' \
  -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"' >/dev/null

has_migrations=$(postgres_psql -d "$db_name" -Atqc \
  "SELECT to_regclass('public.schema_migrations') IS NOT NULL")
migration_state=none
if [[ "$has_migrations" == t ]]; then
  migration_state=$(postgres_psql -d "$db_name" -Atqc \
    "SELECT version::text || ':' || dirty::text FROM schema_migrations ORDER BY version DESC LIMIT 1")
fi
if [[ "$migration_state" != none ]]; then
  IFS=: read -r version dirty <<<"$migration_state"
  [[ "$version" =~ ^[0-9]+$ && "$dirty" =~ ^(true|false)$ ]] || die "preview database $db_name has an invalid migration state"
  [[ "$dirty" == false ]] || die "preview database $db_name has a dirty migration state"
  (( version <= 10#$latest_migration )) || die "preview database $db_name is newer than this worktree's migrations"
fi

mkdir -p -- "$(dirname -- "$shim_file")"
chmod 0700 "$(dirname -- "$shim_file")"
quoted_env_file=$(printf '%q' "$env_file")
umask 077
temporary_shim="$shim_file.tmp.$$"
{
  printf '#!/usr/bin/env bash\nset -a\nsource -- %s\nset +a\n' "$quoted_env_file"
  printf 'PREVIEW_DATABASE_NAME=%q\n' "$db_name"
  cat <<'SHIM'
DATABASE_URL="$(PREVIEW_DATABASE_NAME="$PREVIEW_DATABASE_NAME" python3 - <<'PY'
import os
from urllib.parse import urlsplit, urlunsplit

url = urlsplit(os.environ['DATABASE_URL'])
userinfo = url.netloc.rsplit('@', 1)[0]
netloc = f'{userinfo}@127.0.0.1:5432' if userinfo else '127.0.0.1:5432'
print(urlunsplit((url.scheme, netloc, '/' + os.environ['PREVIEW_DATABASE_NAME'], url.query, url.fragment)))
PY
)"
export DATABASE_URL
unset PREVIEW_DATABASE_NAME
SHIM
} > "$temporary_shim"
chmod 0600 "$temporary_shim"
mv -f -- "$temporary_shim" "$shim_file"

printf 'PREVIEW_DATABASE_NAME=%s\nDEV_ENV_FILE=%s\n' "$db_name" "$shim_file"
printf 'DEVELOPMENT_ENV_SOURCE=%s\n' "$env_file"

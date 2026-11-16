#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: start_stack.sh --repo-root PATH [--dry-run]

Environment:
  DEV_SLUG                  unique lowercase worktree slug
  DEV_DOMAIN_SUFFIX         default: dev.dell.lr-projects.de
  DEV_PREVIEW_ADAPTER       optional compatible launcher path
  DEV_NGINX_PORT            fixed: 18080; the only supported gateway port
  DEV_NGINX_RUNTIME_DIR    shared nginx runtime directory
  DEV_STACK_MAX_SECONDS     default: 86400; maximum: 86400
USAGE
  exit 2
}

die() {
  echo "serve-dev-worktree: $*" >&2
  exit 1
}

repo_root=''
forward_args=()
while (($# > 0)); do
  case "$1" in
    --repo-root)
      (($# >= 2)) || usage
      repo_root=$2
      shift 2
      ;;
    --dry-run)
      forward_args+=(--dry-run)
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

[[ -n "$repo_root" ]] || usage
repo_root=$(cd -- "$repo_root" 2>/dev/null && pwd -P) || die "repository root does not exist"

skill_scripts_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -n "${DEV_PREVIEW_ADAPTER:-}" ]]; then
  launcher=$DEV_PREVIEW_ADAPTER
elif [[ -x "$repo_root/scripts/dev/start-nginx-worktree.sh" ]]; then
  launcher="$repo_root/scripts/dev/start-nginx-worktree.sh"
elif [[ -f "$repo_root/go-server/go.mod" && -f "$repo_root/flutter/pubspec.yaml" ]]; then
  # Older MonaServer branches may not contain the adapter yet. Keep a tested
  # copy with this skill so a freshly checked-out worktree can still start.
  launcher="$skill_scripts_dir/monaserver/start-nginx-worktree.sh"
else
  die "no preview adapter in $repo_root; provide DEV_PREVIEW_ADAPTER or add scripts/dev/start-nginx-worktree.sh"
fi
[[ -x "$launcher" ]] || die "preview adapter is not executable: $launcher"

# Keep generated hostnames short enough for nginx's default server-name hash
# bucket size. A short branch prefix plus path hash differentiates worktrees.
if [[ -z "${DEV_SLUG:-}" ]]; then
  branch=$(git -C "$repo_root" symbolic-ref --quiet --short HEAD 2>/dev/null || printf 'worktree')
  branch=$(printf '%s' "$branch" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g; s/--*/-/g; s/^-*//; s/-*$//')
  branch=${branch:0:3}
  branch=${branch%-}
  branch=${branch:-prv}
  path_hash=$(printf '%s' "$repo_root" | sha256sum | cut -c1-8)
  export DEV_SLUG="$branch-$path_hash"
fi

max_seconds=${DEV_STACK_MAX_SECONDS:-86400}
case "$max_seconds" in
  ''|*[!0-9]*) die 'DEV_STACK_MAX_SECONDS must be a positive integer' ;;
esac
normalized_max_seconds=$(printf '%s' "$max_seconds" | sed 's/^0*//')
normalized_max_seconds=${normalized_max_seconds:-0}
if [[ "$normalized_max_seconds" == 0 || ${#normalized_max_seconds} -gt 5 || ( ${#normalized_max_seconds} -eq 5 && "$normalized_max_seconds" > 86400 ) ]]; then
  die 'DEV_STACK_MAX_SECONDS must be between 1 and 86400'
fi
max_seconds=$normalized_max_seconds

if [[ -n "${DEV_NGINX_PORT:-}" && "$DEV_NGINX_PORT" != 18080 ]]; then
  die 'DEV_NGINX_PORT must be 18080 for serve-dev-worktree'
fi
export DEV_NGINX_PORT=18080

# The repository launcher owns the single timeout/cleanup lifecycle. Wrapping
# it in a second timeout can orphan its nginx child when both timers expire at
# once, so delegate directly after validating the requested maximum.
exec "$launcher" --repo-root "$repo_root" "${forward_args[@]}"

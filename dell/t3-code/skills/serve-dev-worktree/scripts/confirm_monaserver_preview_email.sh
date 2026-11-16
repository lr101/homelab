#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  echo "confirm_monaserver_preview_email: $*" >&2
  exit 1
}

repo_root=
slug=
username=stickitviewer
while (($# > 0)); do
  case "$1" in
    --repo-root) (($# >= 2)) || die '--repo-root needs a path'; repo_root=$2; shift 2 ;;
    --slug) (($# >= 2)) || die '--slug needs a value'; slug=$2; shift 2 ;;
    --username) (($# >= 2)) || die '--username needs a value'; username=$2; shift 2 ;;
    -h|--help)
      printf 'Usage: %s --repo-root PATH --slug SLUG [--username USERNAME]\n' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$repo_root" ]] || die '--repo-root is required'
repo_root=$(cd -- "$repo_root" 2>/dev/null && pwd -P) || die 'repository root does not exist'
[[ "$slug" =~ ^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$ ]] || die 'slug must be lowercase DNS-safe text, at most 48 characters'
[[ "$username" =~ ^[a-zA-Z0-9_.-]{1,255}$ ]] || die 'invalid fixture username'
command -v mise >/dev/null 2>&1 || die 'mise is required to prepare the preview fixture email'
command -v python3 >/dev/null 2>&1 || die 'python3 is required to clean up the temporary helper'

state_dir="$repo_root/.worktrees/preview/$slug"
[[ ! -L "$repo_root/.worktrees" && ! -L "$repo_root/.worktrees/preview" && ! -L "$state_dir" ]] || die 'preview state paths must not be symlinks'
selected_env_file="$state_dir/selected-env-file"
if [[ -f "$selected_env_file" && ! -L "$selected_env_file" ]]; then
  env_file=$(<"$selected_env_file")
else
  env_file=${DEV_ENV_FILE:-$repo_root/.env.dev}
fi
[[ -f "$env_file" ]] || die 'the selected preview environment file is missing'
source_script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
helper_name="preview-confirm-email-${slug//-/_}-$$"
helper_dir="$repo_root/go-server/cmd/$helper_name"
[[ ! -e "$helper_dir" && ! -L "$helper_dir" ]] || die 'temporary helper path already exists'
mkdir -m 0700 -- "$helper_dir"
cleanup() {
  python3 - "$helper_dir" <<'PY'
from pathlib import Path
import sys
directory = Path(sys.argv[1])
(directory / "main.go").unlink(missing_ok=True)
directory.rmdir()
PY
}
trap cleanup EXIT
cp -- "$source_script_dir/monaserver/confirm_preview_email.go" "$helper_dir/main.go"

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
cd "$repo_root/go-server"
mise exec -- go run "./cmd/$helper_name" --slug "$slug" --username "$username"

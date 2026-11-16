#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  echo "seed_monaserver_preview: $*" >&2
  exit 1
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

repo_root=
slug=
api_url=
web_url=
domain_suffix=${DEV_DOMAIN_SUFFIX:-dev.dell.lr-projects.de}
dry_run=false

while (($# > 0)); do
  case "$1" in
    --repo-root) (($# >= 2)) || die '--repo-root needs a path'; repo_root=$2; shift 2 ;;
    --slug) (($# >= 2)) || die '--slug needs a value'; slug=$2; shift 2 ;;
    --api-url) (($# >= 2)) || die '--api-url needs a value'; api_url=${2%/}; shift 2 ;;
    --web-url) (($# >= 2)) || die '--web-url needs a value'; web_url=${2%/}; shift 2 ;;
    --domain-suffix) (($# >= 2)) || die '--domain-suffix needs a value'; domain_suffix=$2; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help)
      printf 'Usage: %s --repo-root PATH --slug SLUG --api-url URL --web-url URL [--domain-suffix DOMAIN] [--dry-run]\n' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$repo_root" ]] || die '--repo-root is required'
repo_root=$(cd -- "$repo_root" 2>/dev/null && pwd -P) || die 'repository root does not exist'
[[ -n "$slug" && "$slug" =~ ^[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?$ ]] || die 'slug must be lowercase DNS-safe text, at most 48 characters'
[[ -f "$repo_root/testdata/seed.mjs" ]] || die 'MonaServer testdata seeder is missing'
[[ "$domain_suffix" =~ ^[A-Za-z0-9.-]+$ && "$domain_suffix" != .* && "$domain_suffix" != *. && "$domain_suffix" != *..* ]] || die 'invalid development domain suffix'

api_host=${api_url#*://}
api_host=${api_host%%/*}
web_host=${web_url#*://}
web_host=${web_host%%/*}
api_scheme=${api_url%%://*}
web_scheme=${web_url%%://*}
case "$api_scheme:$web_scheme" in
  http:http|https:https) ;;
  *) die 'API and web URLs must both use http or both use https' ;;
esac
[[ "$api_scheme" == https ]] || die 'preview email sign-in links require HTTPS'
[[ "$api_host" == "api-$slug.$domain_suffix" ]] || die 'API URL does not match this preview slug and domain suffix'
[[ "$web_host" == "web-$slug.$domain_suffix" ]] || die 'web URL does not match this preview slug and domain suffix'
[[ "$api_url" == "$api_scheme://$api_host" && "$web_url" == "$web_scheme://$web_host" ]] || die 'preview URLs must not include paths, query strings, or ports'

state_dir="$repo_root/.worktrees/preview/$slug"
fixture_dir="$state_dir/fixture"
if [[ "$dry_run" == true ]]; then
  printf 'FIXTURE_OUTPUT_DIR=%s\nPREVIEW_URL=%s/\n' "$fixture_dir" "$web_url"
  exit 0
fi

command -v openssl >/dev/null 2>&1 || die 'openssl is required to generate a test password'
command -v flock >/dev/null 2>&1 || die 'flock is required to safely persist a test password'
command -v mise >/dev/null 2>&1 || die 'mise is required to run the fixture seeder'
command -v python3 >/dev/null 2>&1 || die 'python3 is required to request the preview sign-in link'

password_file="$state_dir/password"
[[ ! -L "$state_dir" && ! -L "$fixture_dir" && ! -L "$password_file" ]] || die 'preview fixture paths must not be symlinks'
mkdir -p -- "$fixture_dir"
chmod 0700 "$state_dir" "$fixture_dir"
exec 9>"$state_dir/seed.lock"
flock 9
if [[ ! -s "$password_file" ]]; then
  temporary_password="$password_file.tmp.$$"
  umask 077
  openssl rand -hex 12 > "$temporary_password"
  chmod 0600 "$temporary_password"
  mv -n -- "$temporary_password" "$password_file"
  [[ ! -e "$temporary_password" ]] || rm -f -- "$temporary_password"
fi
chmod 0600 "$password_file"
test_password=$(<"$password_file")
[[ "$test_password" =~ ^[a-f0-9]{24}$ ]] || die 'stored preview password has an unexpected format'

cd "$repo_root"
mapfile -t fixture_usernames < <(
  mise exec -- node --input-type=module -e '
    import { readFileSync } from "node:fs";
    const scenario = JSON.parse(readFileSync("testdata/scenarios.json", "utf8"));
    for (const user of scenario.users) console.log(user.username);
  '
)
(( ${#fixture_usernames[@]} > 0 )) || die 'fixture scenario has no users'

seed_output_file=$(mktemp "$state_dir/seed-output.XXXXXX")
chmod 0600 "$seed_output_file"
run_fixture_seed() {
  TEST_API_URL="$api_url" \
  TESTDATA_ALLOW_REMOTE_API=1 \
  TESTDATA_OUTPUT_DIR="$fixture_dir" \
  TESTDATA_PASSWORD="$test_password" \
    mise exec -- node testdata/seed.mjs >"$seed_output_file" 2>&1
}

if ! run_fixture_seed; then
  if grep -Eq 'email is not confirmed|exists but the supplied TESTDATA_PASSWORD does not match|failed \(429\)' "$seed_output_file"; then
    echo 'Fixture users need confirmation; confirming only accounts in this isolated preview database and retrying.' >&2
    for username in "${fixture_usernames[@]}"; do
      "$script_dir/confirm_monaserver_preview_email.sh" \
        --repo-root "$repo_root" \
        --slug "$slug" \
        --username "$username"
    done
    if ! run_fixture_seed; then
      cat "$seed_output_file" >&2
      rm -f -- "$seed_output_file"
      exit 1
    fi
  else
    cat "$seed_output_file" >&2
    rm -f -- "$seed_output_file"
    exit 1
  fi
fi
cat "$seed_output_file"
rm -f -- "$seed_output_file"

"$script_dir/confirm_monaserver_preview_email.sh" \
  --repo-root "$repo_root" \
  --slug "$slug" \
  --username stickitviewer

login_url=$(python3 "$script_dir/request_monaserver_preview_login_link.py" \
  --repo-root "$repo_root" \
  --slug "$slug" \
  --api-url "$api_url" \
  --domain-suffix "$domain_suffix" \
  --username stickitviewer)

printf '\nPreview test account\n'
printf 'One-time sign-in link (expires in 24 hours):\n%s\n' "$login_url"
printf 'The link signs in as the Stick-It fixture viewer and can be used once.\n'
printf 'Sample group: Stick-It Fixture - Public Member Group\n'
printf 'That account is a member of the group and can browse its seeded pins.\n'

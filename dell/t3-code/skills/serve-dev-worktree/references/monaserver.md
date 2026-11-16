# MonaServer adapter

Use this adapter when the worktree contains `go-server/go.mod` and
`flutter/pubspec.yaml`. `scripts/start_stack.sh` prefers the worktree's
executable `scripts/dev/start-nginx-worktree.sh` when present; otherwise it
uses the tested adapter bundled with this skill, including its adjacent nginx
renderers. Both launchers receive the selected worktree through `--repo-root`.

## Prepare the native stack

Confirm implementation checks are complete. The native stack needs a local
`.env.dev`, PostgreSQL/PostGIS, RustFS, nginx, mise, Go, Flutter, and the
launcher's command-line tools. The env file must include
`ADMIN_SESSION_HMAC_KEY`. Never print it or include its values in diagnostic
output. A fresh worktree may not contain the ignored file. Use its `.env.dev`
when present; otherwise try sibling worktree `.env.dev` files in sorted path
order with `--env-file`, accepting the first one whose database endpoint passes
the helper's local-only checks. Reuse it read-only and do not copy secrets.
If no candidate works, create an ignored local-only `.env.dev` from the
repository example using fresh credentials, then validate it with the helper.

For every launch, generate a fresh random-suffix slug and set `DEV_SLUG`
explicitly. Before reserving it, check that `.worktrees/preview/<slug>`, the
`ms_preview_<slug-with-hyphens-replaced-by-underscores>` database, and the
shared nginx route for that slug are all unused. Choose a different slug if
any state already exists; never restart a preview under an old slug.

Check `mise.toml` and install only the required pinned tools when absent, for
example:

```bash
cd /absolute/path/to/MonaServer
mise install go flutter
mise exec -- go version
mise exec -- flutter --version
```

Check `nginx`, `rustfs`, `pg_isready`, `runuser`, `openssl`, and the other
commands the launcher reports as required. If a dependency is missing, follow the
`install-dependencies` skill. For native Debian PostgreSQL/PostGIS setup, read
`go-server/internal/db/AGENTS.md`; install OS packages only when needed, using
`apt-get update` immediately before `apt-get install`. If PostgreSQL is
installed but stopped, inspect the installed cluster and start it with
`pg_ctlcluster`, then verify `pg_isready -h 127.0.0.1 -p 5432`.

For previews, do not point the API at a shared development database. Prepare a
worktree-scoped database from the local PostgreSQL instance and a temporary env
shim that changes only the database name:

```bash
/root/.agents/skills/serve-dev-worktree/scripts/prepare_monaserver_preview_db.sh \
  --repo-root /absolute/path/to/MonaServer \
  --slug PREVIEW_SLUG
```

Use `--env-file /absolute/path/to/.env.dev` for the selected sibling
configuration. The helper refuses non-local PostgreSQL endpoints,
creates a persistent `ms_preview_<slug>` database only if absent, assigns the
configured local DB role as owner, enables PostGIS extensions, and rejects
dirty or too-new migration state. It prints a `DEV_ENV_FILE` shim path. Set
that exact path in the environment when launching the stack, for example:

```bash
export DEV_ENV_FILE=/tmp/serve-dev-worktree/env/PREVIEW_SLUG.sh
/root/.agents/skills/serve-dev-worktree/scripts/start_stack.sh \
  --repo-root /absolute/path/to/MonaServer
```

Use the shim path printed by the helper if `XDG_RUNTIME_DIR` changes the
default location shown above. Keep the shim while the preview is running: the
fixture seeder reuses it to confirm the isolated database and can generate a
fresh sign-in link if the previous one expires. Remove only that generated
shim after stopping the preview. Retain the preview database and seed state;
future preview launches use a new slug and a separate database. Do not
truncate, downgrade, or automatically drop a preview database.

The Go server applies its embedded migrations at startup. The adapter starts
per-worktree RustFS, API, Flutter, and nginx routes; do not start duplicate
instances manually.

Before an expensive build, compare an existing database's clean
`schema_migrations` version with the highest migration embedded in the target
branch. If the database is dirty or newer than that branch, do not force its
version, downgrade it, or retry against it. When the user has asked to provision
a local preview database, use a separate worktree-scoped database and keep the
configured database untouched; otherwise ask for a compatible development
database. Never copy or print credentials to make this override—use a temporary
environment-file shim that sources the selected env file and changes only the
database name, then remove the shim after the adapter has loaded it.

Before building Flutter, verify how that branch reads `API_HOST`. The current
adapter passes `--dart-define=API_HOST=...`; this only overrides the app if the
client reads that compile-time define. Older branches may load `API_HOST` from
the bundled `flutter/config` file instead. For those branches, supply the
generated API host through a temporary preview build input, restore any
tracked config immediately after the build, and verify the generated host is
present in the web assets. Never serve a build that still targets the
production API.

The selected MonaServer launcher checks that nginx's `nobody` worker can
traverse the existing path to `DEV_WEB_ROOT` before starting Flutter's
potentially slow build. If it reports a blocked directory, grant `nobody`
execute-only ACLs on the specific parent directories that block traversal;
do not make a private home or worktree world-readable. For example, after
installing the `acl` package if `setfacl` is missing, repeat this for each
blocked parent path:

```bash
setfacl -m u:nobody:--x /path/to/blocked/ancestor
```

If the post-build check reports that `index.html` is unreadable, grant
read/traverse access only to the generated web output, then retry the
incremental build:

```bash
repo_root=/absolute/path/to/MonaServer
setfacl -R -m u:nobody:r-X "$repo_root/flutter/build/web"
runuser -u nobody -- test -r "$repo_root/flutter/build/web/index.html"
```

Replace the example paths with the absolute worktree and only the directories
that are actually blocked. Remove only the temporary `nobody` ACL entries
after stopping the preview if they are no longer needed. The launcher keeps a
post-build file readability check as a final guard.

Start the current worktree with the installed skill helper:

```bash
/root/.agents/skills/serve-dev-worktree/scripts/start_stack.sh \
  --repo-root /absolute/path/to/MonaServer
```

The helper delegates to the worktree's `scripts/dev/start-nginx-worktree.sh`
when available, otherwise to the tested MonaServer adapter bundled with this
skill. It builds Flutter Web, starts the Go API and RustFS on reserved random
loopback ports, starts local SMTP capture, and registers per-worktree routes in
the shared nginx gateway. Confirm its ready output includes
`email_capture=loopback` before requesting a sign-in link. If a repository
launcher lacks capture support, set `DEV_PREVIEW_ADAPTER` to the bundled
launcher after confirming it can target the selected worktree:

```bash
DEV_PREVIEW_ADAPTER=/root/.agents/skills/serve-dev-worktree/scripts/monaserver/start-nginx-worktree.sh \
  /root/.agents/skills/serve-dev-worktree/scripts/start_stack.sh \
  --repo-root /absolute/path/to/MonaServer
```

- `https://web-<slug>.dev.dell.lr-projects.de/` — Flutter Web
- `https://api-<slug>.dev.dell.lr-projects.de/` — Go API
- `https://storage-<slug>.dev.dell.lr-projects.de/` — RustFS S3 API
- `https://console-<slug>.dev.dell.lr-projects.de/` — RustFS console

Flutter receives the generated API URL at build time. The launcher adapts the
Docker-oriented database endpoints in the selected development environment to
local PostgreSQL, keeps RustFS internal traffic on HTTP, and configures the
external storage URL with the public HTTPS hostname.

## Refresh Flutter's offline cache manifest

After every preview build, run the repository's offline-web generator against
the just-built assets:

```bash
cd /absolute/path/to/MonaServer/flutter
mise exec -- dart run tool/generate_offline_web.dart build/web
```

The generated `offline-sw.js` hashes the complete web bundle and names its
cache from that hash. The bundled adapter runs this generator automatically
when the source is present. Repository-specific or explicitly selected
adapters may only run `flutter build web`, leaving the previous service worker
in place while nginx serves the new bundle. Run the generator again after the
adapter reports ready and before reporting the preview links; it is safe to
rerun for adapters that already generate the manifest.

Verify that the public route is serving the regenerated worker before handing
off the preview:

```bash
sha256sum build/web/offline-sw.js
curl --fail --silent --show-error \
  "https://web-PREVIEW_SLUG.dev.dell.lr-projects.de/offline-sw.js" | sha256sum
```

The hashes must match. If a browser still shows old content after this check,
close and reopen the preview. If needed, unregister the service worker and
clear site data for that preview host, then reload.

After the adapter reports readiness, seed the reusable fixture into that
preview's isolated database and generate its one-time sign-in link:

```bash
/root/.agents/skills/serve-dev-worktree/scripts/seed_monaserver_preview.sh \
  --repo-root /absolute/path/to/MonaServer \
  --slug PREVIEW_SLUG \
  --api-url https://api-PREVIEW_SLUG.dev.dell.lr-projects.de \
  --web-url https://web-PREVIEW_SLUG.dev.dell.lr-projects.de
```

The script seeds `stickitviewer`, which is a member of `Stick-It Fixture -
Public Member Group` and can view its sample pins. It stores the seeder password
and fixture state in the ignored `.worktrees/preview/<slug>/` folder with
owner-only permissions, so reseeding the same persistent preview is
idempotent. The preview setup confirms only this isolated fixture email using
the server's email-claim service, requests a normal public email sign-in, and
extracts the generated link from local SMTP capture. It clears captured
messages after extraction and prints the one-time sign-in link instead of the
fixture username or password. The link expires in 24 hours and can be used
once. The API URL must be the exact generated preview host; the helper rejects
unrelated domains.

The adapter keeps the preview email HMAC and encryption keys in the owner-only
`.worktrees/preview/<slug>/email-runtime.env` file so queued delivery payloads
survive a restart. Keep it with that preview state, and never print or copy its
contents.

The launcher enforces one lifetime cap of 86,400 seconds, including graceful
cleanup. `DEV_STACK_MAX_SECONDS` may lower but not raise that cap. The gateway
binds `0.0.0.0:18080`; Traefik should expose only this port. T3's own host
service on port 8080 is unrelated. The shared nginx master renderer sets
`server_names_hash_bucket_size 128` so concurrently registered API, web, and
storage hostnames continue to load with the longest supported preview slugs;
retain that setting when changing the master configuration.

Useful settings include the fresh `DEV_SLUG` selected for this launch,
`DEV_DOMAIN_SUFFIX`, `DEV_ENV_FILE`,
`DEV_NGINX_RUNTIME_DIR`, `DEV_PORT_STATE_DIR`, `DEV_RUSTFS_BIN`, and
`DEV_*_PORT`. `DEV_PREVIEW_ADAPTER` may point to a compatible executable
launcher when the current source branch does not contain the preview scripts;
use this only after confirming that the adapter can target the selected
`--repo-root`. Backend port reservations are process-safe. The launcher leaves
per-slug RustFS data in place when a preview stops.

Read the repository's `docs/development-traefik.md` when wildcard routing,
Traefik labels, DNS, or TLS needs investigation. If refreshing a worktree,
stop only that worktree's launcher/session; never kill the shared nginx master
while another slug is active.

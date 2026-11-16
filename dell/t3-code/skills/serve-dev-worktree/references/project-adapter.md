# Adapter contract for other projects

Project startup is intentionally an adapter, not a framework-guessing routine.
Next.js, Flutter, Go, Rails, and multi-service repositories differ in build
steps, runtime configuration, health checks, and service dependencies. A
shared skill should orchestrate previews and present URLs; each repository
should own the small amount of logic specific to its application.

## Repository interface

Add an executable `scripts/dev/preview.sh` to a project that needs previews.
It should support:

```text
preview.sh start --slug SLUG --domain-suffix dev.dell.lr-projects.de \
  --gateway-port 18080 --max-seconds 86400
preview.sh status --slug SLUG
preview.sh stop --slug SLUG
```

`start` runs in the foreground, starts only that worktree's services, waits for
health checks, then prints one line per public endpoint in this format:

```text
PREVIEW_URL <service-name> <absolute-url>
```

For example:

```text
PREVIEW_URL web https://web-feature-a.dev.dell.lr-projects.de/
PREVIEW_URL api https://api-feature-a.dev.dell.lr-projects.de/
```

The adapter should also:

- Build/configure the app with its generated public service URLs (including
  browser-facing API URLs, CORS origins, OAuth callback URLs, and WebSocket
  origins where applicable).
- Bind app processes to loopback ports, selecting/reserving ports safely when
  multiple worktrees can run at once.
- Register only its own `service-<slug>.dev.dell.lr-projects.de` routes in the
  shared nginx gateway on port 18080. Keep other slugs' routes and processes
  untouched.
- Trap termination, stop only its own child process groups, unregister its
  routes, and honor the 24-hour maximum including cleanup.
- Keep secrets out of command output and logs. Store mutable runtime state and
  logs outside the Git worktree.
- Have `status` report the running revision and URLs, and make `stop` idempotent
  for its slug.

Use `web` for the main browser app and `api` for its API when those roles exist;
add names such as `admin` or `docs` only when useful. The external Traefik
router's HostRegexp must accept the chosen service prefixes, and wildcard DNS
and TLS must cover the resulting single-label hostnames. Do not expose each
random backend port through Traefik.

Keep the MonaServer adapter specialized for its PostGIS, RustFS, and Flutter
configuration. New repositories can adopt the same lifecycle and URL contract
without copying MonaServer's database or build assumptions. If a project does
not yet have this adapter, implement and verify it in that project before
claiming the project is preview-ready.

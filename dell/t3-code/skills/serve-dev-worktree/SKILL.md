---
name: serve-dev-worktree
description: Start and show a live development preview when implementation work is complete in MonaServer or another repository with a preview adapter, including before requested pushes or PR creation.
---

# Preview

## When to preview

For every coding thread that reaches a finished, push-ready state in a
repository this skill can preview, start or refresh that worktree's preview
after implementation checks and show its links before handing the task back.
Push-ready means the implementation is complete and could be offered for
review or pushed; do not wait for an explicit `Preview` or push request. This
applies to backend-only and other non-UI changes too. An explicit `Preview`
request can also start one before the task is complete.

Every preview launch must use a fresh, never-used `DEV_SLUG`. Do not rely on
the launcher's deterministic worktree default and do not reuse an earlier
slug to refresh a preview. Generate a lowercase DNS-safe slug with a random
suffix, check that its preview state, slug-derived database, and shared gateway
route are unused, and choose another if any already exists. Keep each
preview's database and state isolated under its own slug; leave older
previews untouched unless the user asks to stop one.

Do not start a project preview for questions, reviews, planning, or tasks that
only change this skill or other files outside the previewable application.
For an unsupported project with no adapter, do not guess its start command or
claim a preview; explain the gap and ask before implementing an adapter.
Respect a later, explicit instruction not to preview.

If the user requests a push or PR creation/update in the same task, finish
focused checks, start or refresh the preview, verify it, and present its links
before publishing. Then carry out the requested push and/or PR creation/update
in the same task; do not wait for the user to test the preview unless the user
explicitly asks you to wait. If source changes after preview verification,
refresh and report the preview before publishing those changes. Do not refresh
an unchanged preview merely because its already-previewed commit is later
pushed.

Never merge a pull request. If the user asks for a merge, leave the PR open and
report its current status without invoking a merge action.

This skill does not install a webhook or background event listener. An
external GitHub push/PR event does not itself trigger a preview. Do not push or
create a PR just because the user said `Preview`.

The MonaServer worktree adapter builds a Wasm release bundle and starts API,
storage, and routing services. Reserve it for previews that need those fresh
services or release/offline behavior. For a UI-only preview, do not pay that
build cost; use the Flutter debug server and be clear about any API-backed
flows that are unavailable.

Before a requested push, read the target repository's `AGENTS.md` and follow
its pre-push verification checklist. Run focused checks during development if
the repository asks for them, then run every required local check immediately
before pushing. If a required check cannot run, stop before pushing and report
the missing prerequisite and check; proceed only if the user explicitly waives
it.

For `hiring-scraper`, follow `AGENTS.md` and `docs/ci.md`: run the Docker-free
quality checks, frontend build, both browser shards, and disposable local
PostgreSQL checks. Also run `scripts/ci/run.sh` when Docker is available. Use
only an authorized local PostgreSQL test server. Report unavailable checks
accurately; a partial run is not a completed pre-push verification.

After pushing, monitor the relevant GitHub Actions workflows and report
required-check status. Passing checks do not authorize a merge; this skill
always stops with the PR open.

## Choose the project adapter

Prefer the adapter provided by the target repository; do not guess a
framework's start command or silently fall back to Docker. For MonaServer, use
a repository launcher only when it supports the local SMTP capture described
in [references/monaserver.md](references/monaserver.md). If the checked-out
branch does not contain `scripts/dev/start-nginx-worktree.sh` or its launcher
lacks that support, use the tested bundled adapter at
`scripts/monaserver/start-nginx-worktree.sh` through `scripts/start_stack.sh`.
It accepts `--repo-root` and includes its nginx renderers beside it. An explicit
`DEV_PREVIEW_ADAPTER` still takes precedence; verify that it targets the
selected worktree before use.

- For MonaServer, read [references/monaserver.md](references/monaserver.md).
- For another repository, use its `scripts/dev/preview.sh` adapter as described
  in [references/project-adapter.md](references/project-adapter.md).
- If another project has no adapter, inspect its runtime and explain what is
  needed. Implement an adapter only when that work is explicitly in scope;
  otherwise ask before editing or starting guessed commands. Keep project
  build, environment, and service-dependency knowledge in that adapter; keep
  preview triggering, link reporting, and shared-gateway conventions here.

For MonaServer, follow its preview reference to use a worktree-scoped database
and seed the reusable demo account before reporting the preview. Include the
clickable one-time email sign-in link from the preview mail capture in the
final preview message; do not include fixture credentials. Never seed a shared
development database.

The shared gateway is one nginx instance per dev container on port `18080`.
Each worktree gets a unique slug and its own routes; project services should
bind to loopback ports, not ports exposed directly through Traefik. Preserve
other active worktrees when adding, refreshing, or stopping one preview. The
skill launcher makes generated slugs compact enough for nginx host routing;
set `DEV_SLUG` to the fresh slug selected for this launch.

## Prepare dependencies and shared services

Before launching an adapter, read the repository's tool versions, runtime
README, and relevant `AGENTS.md` files. Check each documented prerequisite. If
something is missing, use the `install-dependencies` skill: prefer pinned
project tools and lockfiles, use `apt-get` only for native/system packages, and
run `apt-get update` immediately before an apt installation. Install only what
the selected adapter needs, then verify it.

For a required local database or shared service, follow the repository's
native setup guide, start the service if it is installed but stopped, and check
readiness before launching the app. Confirm the configured database is a
development database. Create a role, database, or extension only when the
project guide calls for it and it is absent; never truncate or repurpose an
existing database as part of preview setup. Leave shared database and nginx
services running when a worktree preview stops; the adapter owns only its
worktree's application processes and routes.

Do not print `.env*` files, development-service credentials, or
service-account files. Keep the MonaServer preview-fixture password in its
owner-only state file and never include it in the preview report. For
MonaServer, use the target worktree's `.env.dev` when present. Otherwise
inspect registered sibling worktrees in sorted path order and reuse the first
`.env.dev` that passes the preview helper's local-endpoint checks. Do not ask
the user to choose among equivalent local candidates. If none works, create a
local-only ignored `.env.dev` from the repository example with generated
credentials and validate it before launching. Never copy a sibling's file or
print its contents. For other projects, if a needed development configuration
or service credential is missing, stop and ask rather than fabricating or
borrowing one from another worktree without clear authorization.

## Start and report

1. Run the checks required for the requested change before starting live
   services. A preview does not replace tests. Install and start documented
   prerequisites first when needed.
2. Start the target worktree at its current `HEAD` using its adapter. Keep the
   launcher attached to a persistent terminal/session and respect its maximum
   lifetime of 86,400 seconds (24 hours).
3. Verify the adapter reports readiness and check the public web URL. Do not
   claim a link works if DNS, Traefik, or TLS prevents verification.
4. Present the full absolute URLs as clickable Markdown links, with a clear
   label for each frontend/API or other useful service. If public routing is
   unavailable, say so and include the local gateway address and required Host
   header when available.

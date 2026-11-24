# Homelab SSH Migration Runbook

Goal: migrate existing working servers in-place to SOPS + age secret management and repo-tracked Compose definitions from a single controller machine over SSH—without installing Git tooling checkouts, SOPS, private age keys, or Python packages on each server. Preserve persistent data where it sits, keep credential values unchanged, preserve WireGuard/DNS host networking, and preserve exact Compose project, named-volume, bind-mount, and running image identities.

## Safety rules

- Never run unreviewed destructive commands, `docker compose down -v`, volume pruning, live database relocation, or blind image upgrades/rebuilds during adoption.
- Never copy or overwrite entire deployment directories containing databases or runtime state. Only allowlisted repo-tracked definitions/config files and private SOPS runtime outputs are staged.
- Keep provider console / direct SSH access and a tested application-data backup before applying changes. Configuration backups and file rollback are **not** a substitute for database dumps.
- Never print, log, source (`source .env`), `eval`, or pass plaintext secrets in SSH or Docker command-line arguments.
- Existing WireGuard, resolver, Docker daemon, and firewall settings are captured read-only into encrypted SOPS snapshots and **never reconfigured or restarted** during application adoption.
- Roll out one host and one (or a small group of) stack(s) at a time—starting with a low-risk host/service.

## 1. Prerequisites

**Controller (your workstation / management device):**

- Git checkout of this repository at the approved commit.
- Python 3 with controller dependencies installed (`pip install -r scripts/requirements.txt`: `PyYAML` and `python-dotenv`).
- `sops` and `age` installed, with `.sops.yaml` configured with public recipients and `SOPS_AGE_KEY_FILE` pointing to an authorized private age identity outside Git.
- OpenSSH client (`ssh`) using your existing `~/.ssh/config` and SSH agent, with **strict host-key verification** (`known_hosts` already populated—`scripts/ssh-migrate.py` always enforces `BatchMode=yes`, `StrictHostKeyChecking=yes`, and `PasswordAuthentication=no`).

**Remote host (target server):**

- Ordinary `python3` (standard library only; no `pip` packages needed).
- Docker Engine and Docker Compose v2 (`docker compose version`), accessible to the SSH user (or via `--docker-sudo` with passwordless `sudo`).
- Optional passwordless `sudo` (`sudo -n`) **only** when using `capture-host --sudo` to read root-only files such as `/etc/wireguard/*.conf` or live `iptables-save`/`nft` rulesets.
- **Not required on remote:** no permanent Git checkout of migration tooling, no `sops` binary, no private age keys.

> Local-only fallback: `scripts/migrate-homelab.py` remains available if you ever need to run read-only `audit`, `capture-host`, or `import-secrets` directly on a machine without SSH.

## 2. Phase 1 — Prepare (read-only audit, host capture, and secret import)

All commands in Phase 1 are **read-only on the remote host** and default to **dry-run** locally (no `.enc.json` files are created until you pass `--apply`). Plaintext secrets stay in SSH pipes and controller memory, never written to unencrypted files or logs.

You can run the combined `prepare` workflow or run `audit`, `capture-host`, and `import-secrets` individually:

```sh
# 1a. Combined dry-run (no local or remote files written):
python3 scripts/ssh-migrate.py prepare \
  --host medion \
  --ssh-target medion \
  --source-root /home/lr/homelab \
  --audit-output /tmp/medion-audit.json \
  --all-stacks --skip-not-deployed --sudo

# 1b. After reviewing the dry-run output, publish the private audit report and encrypted SOPS files:
python3 scripts/ssh-migrate.py prepare \
  --host medion \
  --ssh-target medion \
  --source-root /home/lr/homelab \
  --audit-output /tmp/medion-audit.json \
  --all-stacks --skip-not-deployed --sudo --apply
```

Or step by step:

```sh
# Read-only audit of containers (no Docker env), named volumes, networks, and legacy source hashes:
python3 scripts/ssh-migrate.py audit \
  --host ionos --ssh-target ionos \
  --source-root /home/lr/homelab \
  --output /tmp/ionos-audit.json

# Read-only host/WireGuard/DNS/firewall baseline capture (dry-run by default; add --apply to write):
python3 scripts/ssh-migrate.py capture-host \
  --host ionos --ssh-target ionos \
  --sudo --runtime-firewall iptables --apply

# Read-only legacy .env / credential file import (dry-run by default; dependencies included automatically):
python3 scripts/ssh-migrate.py import-secrets \
  --host medion --ssh-target medion \
  --source-root /home/lr/homelab \
  --stack nextcloud --apply
```

Important Phase 1 behaviors:

- **Root-only files:** If `/etc/wireguard/*.conf` (or `/etc/wireguard/` itself) or firewall files cannot be read by the SSH user, `capture-host` **fails with an actionable error** listing the unreadable paths instead of silently omitting them. Rerun with `--sudo` (passwordless `sudo -n`) or explicitly opt out with `--skip-unreadable` (which warns and records `SKIPPED_UNREADABLE_PATHS_JSON` in the encrypted snapshot).
- **Strict dotenv & literal preservation:** Uses the same conservative parser as `scripts/migrate-homelab.py`—no shell sourcing/interpolation, preserves quoted literals and optional empty values, preserves exact raw bytes (including CRLF) for file values (`APP_BOOTSTRAP_ENV`, `HOMEASSISTANT_SECRETS_YAML`, `GRAFANA_INI`, `AUTORESTIC_ENV`, etc.), rejects duplicate keys, `COMPOSE_*`, unresolved `$` expansions outside single quotes, ambiguous legacy aliases, and multiline env values without `file_only`. Never overwrites existing `.enc.json` files (pass `--skip-existing` when resuming `--all-stacks`).
- **Missing stacks & dependencies:** Explicitly named stacks (`--stack NAME`) automatically include manifest `dependencies` (`immich-kiosk` for `immich-app`; `backup-credentials` for `shared-backup`) and fail if any source file is missing. With `--all-stacks`, missing stacks also fail unless `--skip-not-deployed` is passed—and even then, only stacks with **neither** running/existing containers **nor** legacy source files are skipped and reported explicitly.
- **No credential rotation:** Existing values are encrypted verbatim. Do not rotate database passwords or revoke working Cloudflare keys during adoption.

Inspect the resulting `secrets/<host>/*.enc.json` files (mode `0600`) before committing them to confirm every non-empty value is `ENC[...]` ciphertext with SOPS metadata. Keep audit reports private outside Git.

## 3. Phase 2 — Tested application-data backup gate (mandatory)

Before running `apply`, take and **restore-test** application-data backups (PostgreSQL/MariaDB dumps, Immich/Nextcloud/Home Assistant/Actual Budget data, autorestic snapshots) and record the snapshot ID / restore-test evidence.

- `scripts/ssh-migrate.py` does **not** back up live databases for you and does **not** claim that configuration/file rollback restores database contents.
- `apply` refuses to touch the remote host unless you explicitly pass **both** `--confirm-backup-tested` and non-empty `--backup-evidence "<snapshot ID and restore test reference>"`.

## 4. Phase 3 — Plan (read-only identity, mount, image, and secret validation)

Generate a private, integrity-hashed adoption plan outside the repository:

```sh
# Plan one or more stacks (covers secret-managed, shared, and credentialless stacks like jellyfin/actual-budget/glance):
python3 scripts/ssh-migrate.py plan \
  --host medion --ssh-target medion \
  --source-root /home/lr/homelab \
  --stack nextcloud --stack jellyfin \
  --plan /tmp/medion-plan.json

# Or plan all currently deployed stacks on a host:
python3 scripts/ssh-migrate.py plan \
  --host medion --ssh-target medion \
  --source-root /home/lr/homelab \
  --all-stacks --skip-not-deployed \
  --plan /tmp/medion-plan.json
```

`plan` makes **zero remote changes** and writes **zero plaintext secrets** into the plan file (only SHA-256 hashes of `.enc.json` inputs and expected runtime outputs). Before writing the plan, it validates every selected stack:

- **Compose project identity:** Records the exact audited `com.docker.compose.project` label and passes `-p <project_name>` at apply time. Fails safely if containers in the directory have missing or conflicting project labels, or if a stack's `container_name` is running from a different directory.
- **Named volume identity (no accidental empty volumes):** Every audited named volume must exist in `docker volume ls` and match the repo Compose destination. The plan generates `.homelab-adopt.override.yml` pinning `volumes: {<key>: {external: true, name: <audited_volume_name>}}`. If any proposed named volume does not match an existing audited volume, `plan` fails safely.
- **Bind mount identity:** Resolves `${VAR}` (using in-memory decrypted values on the controller), remote `~/...` (using remote `$HOME`), and relative `./...` paths, confirming every audited non-secret bind mount preserves the exact same host `Source` and container `Destination`, and every proposed data bind source already exists on the remote host.
- **Exact running image preservation:** Pins each running service to its audited local Docker image ID in `.homelab-adopt.override.yml` and runs `up -d --no-build --pull never`. Fails safely if the repo Compose image tag drifted from the running container unless you explicitly pass `--pin-running-images`. Services with `build:` (such as `dell/t3-code` and `medion/nextcloud`) are pinned to their running image ID and never rebuilt during adoption.
- **One-shot schema migrations blocked:** Any stack defining a `restart: "no"` service or `depends_on: ... condition: service_completed_successfully` (such as `medion/hiring_scraper`'s `migrate` service) is blocked from automatic adoption so schema migrations never run unattended.
- **New/removed services:** Fails safely if the repo removes a running service or introduces a new service (such as `ionos/traefik`'s `socket-proxy`) unless `--allow-new-services` is explicitly passed after pre-pulling the new image locally.
- **Stale/tampered plan protection:** Records `plan_digest`, Git `HEAD` SHA, manifest SHA-256, every staged repo file SHA-256, every `.enc.json` SHA-256, every expected rendered secret SHA-256, and a `remote_state_fingerprint` over audited containers, mounts, volumes, and remote file hashes.

## 5. Phase 4 — Apply (in-place cutover with preflight & automatic rollback)

Review the plan summary, then execute cutover with your tested backup evidence:

```sh
python3 scripts/ssh-migrate.py apply \
  --plan /tmp/medion-plan.json \
  --confirm-backup-tested \
  --backup-evidence "autorestic snapshot 2026-11-24 verified restore into temp dir"
```

Before modifying a single file or container on the remote host, `apply`:

1. Verifies `plan_digest` (rejects edited or corrupted plans).
2. Verifies the backup gate (`--confirm-backup-tested` + `--backup-evidence`).
3. Verifies controller repo `HEAD`, manifest, staged repo files, and re-decrypted `.enc.json` outputs still match the plan hashes (rejects repo or secret drift).
4. Runs a read-only remote `preflight` across **all** selected stacks confirming zero remote state drift, all pinned local images and named volumes exist, target directories are writable, and the rollback directory is fresh.

Then, for each stack sequentially:

1. Creates a protected mode-`0700` rollback snapshot (`~/.homelab-migration-rollback/<host>/<plan-id>/`) containing mode-`0600` copies of every file to be overwritten or archived, their original permissions, original Compose file paths, and `index.json`/`state.json`.
2. Stages allowlisted repo files and private runtime secrets atomically (mode `0600` files, mode `0700` `.secrets/` directories), archiving superseded legacy `.env` / credential files into the rollback snapshot so direct passwords do not conflict with `_FILE` secrets. In-place secret paths (such as `thinkpad/ha/config/secrets.yaml`, `medion/logging/grafana-config/grafana.ini`, and `<host>/.autorestic.env`) are backed up and atomically replaced in place.
3. Loads `.secrets.env` (raw `KEY=value` lines, no shell eval or `--env-file`) into the Compose process environment and validates `docker compose ... config --quiet` before touching containers.
4. Runs `docker compose -p <project> -f <compose...> -f .homelab-adopt.override.yml up -d --no-build --pull never`.
5. Verifies all previously running services are running.

If **any** step fails on any stack (including a failure on the 2nd or $N$-th stack), `apply` automatically performs best-effort rollback across the failed stack and every previously applied stack in reverse order, redacting any secrets from error output.

## 6. Phase 5 — Verification and explicit rollback

After `apply` succeeds, verify each adopted stack beyond `running` status:

- Application login, existing data visibility, Traefik HTTPS routing, and health checks.
- Next scheduled or manual backup hook execution (verify DB dumps succeed with `_FILE` secrets).
- On IONOS or hosts with WireGuard/AdGuard, verify WireGuard handshakes/routes and DNS lookups (host + container).

If you need to revert after an applied cutover, run explicit recovery over SSH:

```sh
# Revert all stacks applied by a plan:
python3 scripts/ssh-migrate.py rollback --plan /tmp/medion-plan.json

# Or revert a single stack from a specific remote rollback directory:
python3 scripts/ssh-migrate.py rollback \
  --ssh-target medion \
  --rollback-dir /home/lr/.homelab-migration-rollback/medion/<plan-id> \
  --stack nextcloud
```

`rollback` restores backed-up files and archived legacy `.env` files with their original permissions, deletes newly materialized `.secrets*` files and `.homelab-adopt.override.yml`, and runs `docker compose up -d --no-build --pull never` with the restored original Compose files. **If a service wrote to or migrated its database after cutover, restore from your tested application-data backup rather than relying solely on file rollback.**

Retain the protected rollback directory for an agreed soak window before removing old copies.

## 7. What is automated vs. what requires manual intervention

| Area | Automated by `scripts/ssh-migrate.py`? | Notes / Manual Action |
| --- | --- | --- |
| Remote container/volume/network/source audit | Yes (`audit` / `prepare`) | Excludes Docker `Config.Env` and arbitrary labels |
| Host WireGuard/Docker/DNS/firewall baseline capture | Yes (`capture-host` / `prepare`) | Read-only capture to `secrets/<host>/host-config.enc.json`; use `--sudo` for root-only files |
| Legacy `.env` & credential file SOPS import | Yes (`import-secrets` / `prepare`) | Controller-side SOPS encryption; strict dotenv parsing; raw CRLF preservation for files |
| In-place adoption of standard managed, shared, and credentialless stacks | Yes (`plan` + `apply`) | Preserves project name, named volumes, bind paths, and exact running image IDs |
| Protected rollback copies & SSH rollback | Yes (`apply` auto-rollback + `rollback`) | Restores config/files/Compose definitions; does not undo DB writes |
| Application-data backups & restore testing | **No (manual prerequisite)** | Mandatory operator gate (`--confirm-backup-tested --backup-evidence ...`) |
| Stacks with one-shot schema migrations (e.g. `medion/hiring_scraper`) | **No (intentionally blocked)** | Secrets can be imported via `import-secrets`; review `HIRING_APP_IMAGE`/`HIRING_DB_IMAGE`, back up `./hiring_db`, stage files, and run `migrate` + stack manually |
| Image upgrades, container rebuilds (`build:`), or schema upgrades | **No (intentionally blocked)** | Perform separately after adoption and backups (`--pin-running-images` pins old images if repo tags drifted) |
| Newly added services (e.g. `ionos/traefik` `socket-proxy`) | Opt-in (`--allow-new-services`) | Pre-pull the new image on the host, review, then rerun `plan --allow-new-services` |
| Cloudflare global API key -> zone-scoped `CF_DNS_API_TOKEN` | **No (manual)** | Create and validate a zone-scoped token separately before retiring legacy global keys |
| Host WireGuard / DNS / firewall reconfiguration & reboots | **No (intentionally untouched)** | Captured read-only in `host-config.enc.json`; never edited or restarted during adoption |
| Live database/volume relocation (e.g., to `/srv/homelab`) | **No (manual maintenance)** | Quiesce writes or use native backup/restore in a separate maintenance window |
| Fresh deployment of never-deployed stacks | **No** | Provision via `scripts/sops-secrets.py template` + `sops` editor, then deploy explicitly |
| Later local restarts on the host without controller | Manual / `sops-secrets.py --cached` | Re-run controller `plan`/`apply`, or if using a host checkout, `sops-secrets.py compose --cached` (never run raw `docker compose` without `.secrets.env` interpolation env) |

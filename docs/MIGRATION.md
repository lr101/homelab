# Migration runbook and agent handoff

Goal: adopt the existing working servers into a small Ansible + Compose + SOPS setup. Start with IONOS. Preserve data, credential values, WireGuard identities, DNS behavior, and application/project identities before making improvements.

This PR supplies SOPS runtime integration and a non-destructive migration helper. **It does not supply or apply Ansible playbooks yet.** Sections below define the next agent's implementation and execution contract. Current deployments can continue using the local SOPS wrapper; controller-only Ansible deployment is a subsequent step, not a capability already implemented here.

## Scope and invariants

- Work from the current `main` revision plus this PR. Record both the source revision and deployed image IDs/digests in the migration log.
- No new database passwords, WireGuard keypairs, application initialization, image upgrades, or data-directory moves during adoption.
- Preserve actual Compose project names and existing volume names. A new project name can select an empty new volume even though the old volume still exists.
- Keep provider console/public SSH access and a protected rollback copy before touching IONOS networking. Do not rely on the VPN being changed as the only access path.
- Use explicit inventories and allowlisted files. Never recursively mirror/delete a directory mixing Compose definitions and live data. Never run `docker compose down -v`, volume pruning, or a whole-host firewall flush during migration.
- Keep private age identities, backup repository passwords, and recovery access outside Git. Public age recipients and encrypted files may be committed.

## 1. Audit the actual host before planning changes

Every helper command operates on the machine where it is executed. `--host` selects a manifest entry; it is **not an SSH target**. Run audit/capture on the actual server via your existing SSH/console access. Use a securely staged source tree for controller-side credential imports if preferred. Check the audit report's `executing_hostname` against the intended machine.

Install the pinned Python dependencies from `scripts/requirements.txt` in a virtual environment and SOPS using its official release. Encryption needs **public** recipients only; a migration host does not need a private age key just to export existing credentials.

Use a separate checkout of this PR for the tooling, leaving the deployed checkout intact. A temporary tooling checkout does not imply a permanent Git checkout is required once Ansible push deployment is implemented.

```sh
# Examples: replace paths and host with actual values.
python scripts/migrate-homelab.py audit --host ionos \
  --source-root /home/lr/homelab_templates \
  --output "$HOME/ionos-before.json"
```

The audit reads running and stopped Docker container metadata using a limited inspect projection. It records project/service labels, configured images and image IDs, working/config-file paths, mounts, networks, and running state. It never requests Docker's environment fields or dumps application logs. It also records available interface addresses, routes, policy-routing rules, forwarding state, and WireGuard interface names.

The output must be outside both repository trees; it is created mode `0600` and is never overwritten. Network topology and filesystem paths are still private operational information. Do not paste the full report into public issues. Inspect the `errors` list: unavailable commands/permissions produce an incomplete report, not evidence that the missing state is unimportant.

Additional manual facts to record privately:

- OS/distribution, SSH/sudo access, public NIC and default gateway, cloud firewall rules, resolver owner (systemd-resolved, NetworkManager, resolvconf, or another manager).
- Active WireGuard peers/AllowedIPs versus the on-disk configuration, peer routes, and recent handshakes. Inspect without publishing private keys. Unsaved runtime peer changes must be captured before adoption.
- DNS answers for one public name and each important private zone from both host and a representative container. Test UDP and TCP DNS.
- Docker daemon version, firewall backend, network subnets, the upstream DNS used by AdGuard, and whether the host can resolve/recover while AdGuard is stopped.
- Registry digests for running images and any locally built images that require a saved image/build provenance for rollback.
- External photo/file mounts, named volumes, ownership, scheduled jobs, existing backup health, and IONOS provider firewall state. The helper cannot read the cloud control-plane configuration.

## 2. Capture host configuration encrypted, including IONOS custom entries

Configure `.sops.yaml` using the public recipients described in [SECRETS.md](SECRETS.md). Authorize the deployment controller and a separate offline recovery identity for the required hosts. Private host recipients can remain if local recovery is desired; do not copy the controller's private identity onto servers.

```sh
# Default: report the planned encrypted capture without writing it.
python scripts/migrate-homelab.py capture-host --host ionos

# Run with the file/command privileges needed on the source host.
# Choose the backend actually in use; omit the option for file-only capture.
python scripts/migrate-homelab.py capture-host --host ionos \
  --runtime-firewall iptables --apply
```

`capture-host` reads allowlisted WireGuard configs, Docker daemon config, sysctl files, common resolver/network-manager configuration, and persistent UFW/nftables rule files. With `--runtime-firewall iptables`, it additionally encrypts the output of `iptables-save` and `ip6tables-save`; with `nftables`, it encrypts `nft list ruleset`. Those queries do not change rules. Failed runtime queries abort publication.

The resulting `secrets/ionos/host-config.enc.json` is an encrypted snapshot, **not a restore script or Ansible variable schema**. `SOURCE_PATHS_JSON` maps each encrypted `FILE_...` key to its original absolute path. Live firewall captures must be translated into owned, persistent rules; do not blindly replay Docker-created chains. Empty configuration files are preserved as empty values, consistent with SOPS.

No private configuration is printed or written to a plaintext staging file. The helper refuses existing encrypted destinations; use SOPS to edit/reconcile an existing capture deliberately. Capture files on the actual host, or use `--system-root /path/to/private/export` for a securely staged filesystem tree. The runtime firewall flag always queries the machine executing the tool, not the staged tree.

## 3. Back up and prove recovery before cutover

Take a successful encrypted backup of application files and application-consistent database dumps. A raw copy of a running database directory is not the migration restore plan. Record the snapshot ID, paths, application/database versions, and backend in a private migration log. Restore a representative database and file into an isolated directory/project and test actual content.

Preserve existing paths initially. Check that backup plans include external Immich uploads, generated database dumps, named-volume data where applicable, and file credentials. The current hiring-scraper on `main` uses its custom database image and `./hiring_db` bind mount, not the older Postgres/Redis/Celery stack. Do not resurrect that older deployment. Its app bootstrap environment file must be preserved verbatim. Inspect actual project/container names before relying on the default `hiring_scraper-db-1` backup hook.

Keep a recovery kit separately accessible: age recovery identity, restic repository passwords, SSH/backend access, provider console access, encrypted Git snapshot, and minimal DNS/Traefik configuration. If the NAS is in the same building, ensure an independent off-site copy exists and test its access without the VPN/server being recovered.

## 4. Import current application credentials without rotating them

Each entry in `shared/secrets/deployments.json` has `migration.env_files` and `migration.file_values` describing its **legacy** paths relative to the deployed checkout. Review those against the audit first. File-value mappings preserve JSON/YAML/INI and bootstrap dotenv text verbatim. Values consumed by `_FILE` that already exist as dotenv keys require no separate legacy file.

```sh
# Inspect/validate one stack: no SOPS command runs and no files change.
python scripts/migrate-homelab.py import-secrets --host medion --stack nextcloud \
  --source-root /home/lr/homelab_templates

# After reviewing paths, recipients, and backup status, encrypt that stack.
python scripts/migrate-homelab.py import-secrets --host medion --stack nextcloud \
  --source-root /home/lr/homelab_templates --apply
```

The importer does not source shell files, interpolate environment variables, print values, edit source files, or restart services. It preserves quoted literals and optional empty values, rejects duplicate keys and ambiguous expansion syntax, validates required inputs, encrypts through stdin, then atomically creates a new mode-`0600` encrypted file. It refuses existing destinations, missing inputs, escaping symlinks, ambiguous legacy aliases, and unsupported multiline environment values. Partial or guessed imports are not accepted.

If a source uses `${...}`, bare dollar expansion, shell substitutions, or unsupported escapes outside single-quoted literals, make a protected reviewed copy **outside Git**, resolve the actual effective values without executing untrusted input, and point `--source-root` at that prepared tree. Do not globally strip dollar signs or quote characters from passwords. Full-file values such as `APP_BOOTSTRAP_ENV` and `AUTORESTIC_ENV` remain verbatim; confirm their consumers' behavior separately.

For a newly introduced stack that has never been deployed, there are no legacy credentials to import. Skip the importer and use the SOPS template/editor workflow to provision that stack separately; do not invent replacement values for existing services.

Run imports one stack at a time. Import dependencies too (`immich-kiosk` for Immich and `backup-credentials` for shared backup). Copy only the resulting encrypted files to the controller's checkout and review that they contain ciphertext and SOPS metadata before committing. Audit reports and old plaintext remain private.

The importer does not convert a legacy Cloudflare global API key into a zone-scoped token. Provision and validate a scoped token separately if `CF_DNS_API_TOKEN` is missing. Do not revoke a working key during the data-adoption step.

## 5. Agent implementation brief: small push-based Ansible layer

Implement this only after the preceding inventory and restore gate is complete. The desired design is:

```text
ansible/
  inventory.yml                 # Actual SSH endpoints and connection users
  host_vars/ionos.yml            # Public NIC, VPN IPs, DNS policy, existing paths
  site.yml                      # Host configuration only
  deploy.yml                    # Selected application deployment only
  roles/{baseline,wireguard,dns,docker,firewall,compose}/
```

- Controller: approved Git revision, Ansible, SOPS, authorized private age identity, SSH/sudo access. Decrypt on the controller with secret tasks using `no_log: true` and `diff: false`. Transfer only that host's required files using explicit source/destination/mode mappings. Do not transfer the whole repository or private age keys.
- Targets: Docker/Compose, managed files, persistent application data. Use existing data paths for the first deployment. Copy definitions separately, for example to `/opt/homelab/STACK`, while preserving explicit absolute bind paths and existing named-volume/project identities.
- Preserve the semantics of raw `.secrets.env` and file-only values. The Compose process must also receive the selected interpolation values through Ansible's task environment, with logs suppressed. Do not source raw secret files or pass them to `--env-file` and accidentally reinterpret `$`/quotes. Preserve the exact bootstrap dotenv file required by hiring-scraper.
- Host roles: adopt existing WireGuard addresses/keys/peers/AllowedIPs/MTU; one owner for persistent NAT/forwarding rules; scoped sysctl settings; one complete Docker `daemon.json` that preserves DNS, runtimes, and proxy settings; resolver configuration through its actual OS owner. Do not manage `/etc/resolv.conf` blindly or change Docker's firewall backend during adoption.
- Networking checks: avoid overlapping VPN/Docker subnets, DNS loops, reliance on VPN-only DNS to establish the VPN, and publicly exposed port 53. Preserve Docker service-name resolution and test private/public upstream lookups. Do not assume UFW alone filters Docker-published ports.
- Separate host runs from application runs. Restart/reload only when owned config changes. Apply one host/service at a time, with a reviewed recovery path. Do not add automatic timers or CI deployment credentials for this small initial setup.
- Keep the current SOPS manifest as the source of app secret inputs/outputs; avoid a second independently maintained secret map in Ansible. `host-config.enc.json` is an adoption snapshot to review and translate, not an instruction to overwrite every captured file.

Before changing project directories, explicitly set each Compose project name to the audited current value and map each named volume to the audited existing name. Compare resolved mount destinations/sources with the old inventory using secret-safe projections. Do not assume recreating a service reconnects to the old database automatically.

## 6. Cutover, verification, rollback, and completion record

The human/agent executing the migration should keep a private record per host/stack:

```text
source Git revision / deployed image digests:
old and proposed Compose project names:
old and proposed mounts/volume names:
backup snapshot and tested restore result:
provider-console/SSH recovery path:
files changed and previous copies:
checks before/after/reboot:
rollback action and result:
remaining manual work:
```

Pre-cutover identity gate (stop if any answer is unknown or differs):

- Audited existing project label equals the explicit Ansible `project_name` (or the reviewed explicit project name used for a manual Compose command).
- Every proposed named volume resolves to the exact audited existing volume name, not just the same logical key in YAML.
- Every proposed database/file bind mount resolves to the same existing absolute source path and container destination; check file ownership too.
- The intended images/digests match the recorded running versions. No pull/build upgrade is bundled into adoption.

The importer deliberately rejects `COMPOSE_*`; carry project identity in the explicit deployment plan, not secret values. Do not invoke the local wrapper from a relocated checkout until project identity has been accounted for. `config --quiet` checks syntax but does not establish these identity invariants.

First run in check/diff mode with secret output suppressed. Review changes before any host/firewall task. Apply the least critical app first. Verify data, login, health, and the next backup. For IONOS, verify SSH, WireGuard reachability/handshakes/routes, DNS from host and a representative container (public/private/service names), external DNS exposure, and HTTPS routing. A service being `active` alone is not acceptance evidence.

Only after those pass, reboot through a controlled maintenance window while retaining console access and repeat the checks. Verify a second Ansible run makes no changes. Stop and roll back if networking, DNS, data visibility, or backups regress. Roll back config/images and reconnect to the original data; if writes/schema changes occurred, use the tested compatible database snapshot instead of blindly downgrading an image.

Do not remove old directories, volumes, keys, or credential copies during cutover. Retain them securely for an agreed rollback window. A later data move to `/srv/homelab` is a separate maintenance task: quiesce writes or use application-native backup/restore, preserve ownership, verify, and retain the original until acceptance.

This runbook is complete when another operator can reconstruct a host from the recorded Git revision and independently available keys/backups, without relying on the previous server or a single person's shell history.

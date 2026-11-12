# Infisical: setup, migration, and adding deployments

Infisical runs on Dell at `https://secrets.dell.lr-projects.de`. It stores application secrets centrally. A small Python deployment tool authenticates with a read-only host machine identity, fetches secrets over verified HTTPS, and writes private local files. No Infisical client, token, or network access is needed inside application containers.

This is a deployment-time integration, not live rotation. A normal deployment refreshes secrets and fails if Infisical is unavailable. Existing containers keep running, and an explicit `--cached` mode supports recovery using previously synchronized files. Synchronization does not restart services.

## 1. Prepare Dell and bootstrap the manager

Requirements: Docker Compose **2.30+** (`env_file.format: raw`), Python **3.9+** on deployment hosts, working WireGuard/DNS, the external `frontend` Docker network, and Dell Traefik. Tests require OpenSSL; the deployment tool uses only Python's standard library.

Create a DNS record for `secrets.dell.lr-projects.de` pointing to Dell's VPN-reachable address. Do not add a public forwarding route on Ionos. The route accepts the existing `10.217.236.0/24` VPN subnet and loopback addresses; adapt the allowlist if your actual VPN changes. Check source IP handling on the host. Avoid trusting arbitrary forwarded client IP headers.

From the repository root:

```sh
python3 scripts/infisical-secrets.py bootstrap
```

This creates `dell/infisical/.env` with new random `ENCRYPTION_KEY`, `AUTH_SECRET`, and a URL-safe database password, mode `0600`. It refuses to overwrite an existing file. **Make an independent, encrypted offline copy before deployment.** The database cannot recover encrypted secrets without its encryption key.

Infisical's bootstrap keys, its database credentials, and host machine-identity client secrets must remain available outside Infisical. Never put their only copies in the service they unlock.

```sh
cd dell/infisical
docker compose config --quiet
docker compose up -d
```

Infisical uses native authentication; the route is intentionally not behind Pocket ID's interactive OIDC middleware, which would block machine API logins and introduce a recovery dependency. Create the first administrator over the VPN, configure MFA if available, and disable public signup in the instance settings after setup. SSO and some other capabilities depend on Infisical licensing; this integration needs no licensed SSO feature. Keep a native administrator recovery login outside Pocket ID.

If Dell's existing Traefik is already running, leave it running while bootstrapping and importing the Traefik secrets. It discovers the new route automatically. For a completely new host or cold restore, provision the Traefik recovery cache first: `.secrets.env` with its literal `SECRET_SALT`, `CLIENT_ID`, and `CLIENT_SECRET` lines, and `.secrets/cf_dns_api_token` with its Cloudflare token. Use directory mode `0700`, file mode `0600`, and start Traefik using the explicit cached command below. These initial values must come from your offline recovery kit, not from a manager that is not yet reachable.

PostgreSQL and Redis have no published ports and use a separate internal backend network. The API joins the existing `frontend` network for Traefik routing; other containers on that network can reach its native authentication endpoint directly. The VPN middleware protects ingress, not isolation between frontend containers. Infisical has no Docker socket mount. Set `INFISICAL_TRUSTED_PROXY_CIDRS` in its bootstrap `.env` to the actual subnet containing Traefik, rather than retaining the broad default unnecessarily. Optional Infisical settings, such as SMTP, can also be added to that local `.env`.

## 2. Create projects and machine identities

Use one project per host: `homelab-dell`, `homelab-medion`, `homelab-thinkpad`, `homelab-ionos`, and `homelab-synology`, each with a `prod` environment. This isolates hosts without depending on paid/custom folder-role features. Use a separate Universal Auth machine identity for each host, with read-only secret-value access to **only its project**. Keep human administration separate. Configure sensible token TTLs and IP restrictions appropriate to the host's actual API source address.

Create the secret folders listed in [deployments.json](../shared/infisical/deployments.json). Paths include the hostname even though projects are separate, for example `/medion/nextcloud` and `/medion/shared/watchtower`. The tool requests one exact folder at a time, without recursion, personal overrides, or imported folders. Secret references are expanded by Infisical and must be readable by the machine identity. For a first migration, use direct secret values.

On each host, place a copy of [host-config.example.json](../shared/infisical/host-config.example.json) at `/etc/homelab/infisical-HOST.json`, replacing the host, project ID, and client ID. Place the Universal Auth client secret in the file named by `client_secret_file`, using a secure editor or password-manager export. Make that secret file mode `0600`, owned by the account running the tool. Restrict `/etc/homelab` to that account. Do not pass client secrets as command-line arguments, paste them into shell history, or commit them.

The config's `host` must match `--host`. TLS verification is always enabled; install your private CA in the host trust store if needed. HTTP URLs and redirects are rejected. The API uses ordinary Infisical Universal Auth and the v4 secrets API; no extra SDK or CLI is installed in applications.

## 3. Migrate existing deployments without changing credentials

Migrate one stack at a time. Record its running images and take a recoverable backup. **Import the existing credential values; do not generate new database passwords as part of this migration.** Existing databases and application-owned configuration do not automatically change when an input file changes.

For each managed stack:

1. Read its manifest entry. Import the existing `.env` key/value pairs into the matching Infisical folder. Remove dotenv syntax around values: store the actual value, not surrounding quotes, escaped dollar signs, or `KEY=` text. Initially include nonsecret runtime settings from that file too, so no application settings disappear. Local nonsecret `.env` files remain optional and are still read where previously used.
2. Add file-only values from the table below. Store the **complete original text** for JSON, YAML, TOML, INI, and autorestic environment files. Do not base64-encode these unless the application itself expects base64. File-only values are excluded from generated environment files.
3. Synchronize the stack and validate with the wrapper. The required values and file inputs must be present. Compare behavior before restarting.
4. Move migrated credentials out of the old local `.env` into a protected recovery copy **before recreating containers**, especially Cloudflare token values that now use `_FILE`. Keeping both a password/token and its `_FILE` setting can make an image refuse startup. Keep nonsecret paths, identifiers, and optional settings there if useful. Deploy using the wrapper, then verify representative application behavior and backup hooks.
5. Recreate the affected containers to apply changes; updating a secret file does not update environment variables in an existing container. Compose bind-mounted secrets can also require recreation after atomic file replacement.

### Managed file inputs

| Stack | Infisical keys / original content | Result |
| --- | --- | --- |
| All four Traefik stacks | `SECRET_SALT`, `CLIENT_ID`, `CLIENT_SECRET`; `CF_DNS_API_TOKEN` | OIDC environment plus a mounted Cloudflare token file. Use a separate zone-scoped DNS token for each host; replace legacy global API-key auth and revoke those keys after verification |
| Medion / hiring-scraper | `POSTGRES_PASSWORD` | PostgreSQL password file; workers/web still receive the password for their existing database URL |
| Medion / Immich | `DB_PASSWORD` | PostgreSQL password file; Immich still receives its existing app environment |
| Medion / Immich kiosk | Original `kiosk.env` values in `/medion/immich-app/kiosk` | `.secrets.kiosk.env`; synchronized as a dependency of `immich-app` |
| Medion / Nextcloud | `NEXTCLOUD_DB_ROOT_PASSWORD`, `NEXTCLOUD_DB_PASSWORD` | File delivery to MariaDB and Nextcloud; root password does not enter the generated application environment |
| Thinkpad / Home Assistant | `MARIADB_ROOT_PASSWORD`, `MARIADB_PASSWORD`; `HOMEASSISTANT_SECRETS_YAML` containing existing `config/secrets.yaml` | Database password files and HA's existing `!secret` configuration. Preserve the real `MARIADB_USER` / `MARIADB_DATABASE` values; defaults are `homeassistant` |
| Thinkpad / PostGIS | `PG_PASSWORD`; keep `PG_USER` available | PostgreSQL password file |
| Medion / logging | `GF_SECURITY_ADMIN_PASSWORD`; `GRAFANA_INI` containing the existing `grafana-config/grafana.ini`; other existing logging environment values | Grafana admin password file and full existing INI, including any configured SSO credentials |
| Thinkpad / MonaServer | `FIREBASE_TOKEN_JSON` containing the existing Firebase credential JSON; existing `.env` values | Private JSON mount plus existing app/RustFS environment |
| Thinkpad / IWI bulletin board | `IWI_SECRET_JSON` containing the existing `secret.json` | Private read-only credential JSON mount |
| Thinkpad / Garage | `GARAGE_CONFIG_TOML` containing existing `config/garage.toml` | Private complete Garage configuration |
| Each host / backup-credentials | `AUTORESTIC_ENV` containing the **complete** existing host `.autorestic.env` | The existing host backend-password file, synchronized as a dependency of `shared-backup` |

Other existing secret-bearing Compose stacks—T3 Code, AdGuard exporters, Pocket ID, tempserver, shared Watchtower/Telegraf/backup, and Synology Telegraf—use `.secrets.env`. The manifest specifies their exact locations. Shared entries have separate per-host folders even though their generated files live under `shared/`; use a separate repository checkout on each machine, not one shared writable checkout for multiple hosts.

Stacks without deployment-time credentials (such as Jellyfin, Actual Budget, and the homepage) need no artificial secret injection. Application-owned password hashes, session databases, AdGuard configuration, ACME state, SSH private keys, rclone config, and Docker registry credentials remain protected application/host state and encrypted backups; this tool does not replace or rotate them. Add explicit file mappings later if you decide to centrally manage a particular configuration file.

## 4. Synchronize and deploy

From the repository root, using the identity's host name:

```sh
# Initial full-host migration: all required folders must already be populated.
python3 scripts/infisical-secrets.py sync --host medion

# Update one stack, automatically synchronizing its dependencies first.
python3 scripts/infisical-secrets.py compose --host medion --stack nextcloud -- config --quiet
python3 scripts/infisical-secrets.py compose --host medion --stack nextcloud -- up -d

# Medion's shared-backup entry includes the Immich upload override.
python3 scripts/infisical-secrets.py compose --host medion --stack shared-backup -- up -d
```

All selected API folders are fetched and validated before local files are changed. Each file is replaced atomically with mode `0600`; `.secrets` directories use `0700`. A later API failure leaves all existing files alone. A filesystem failure while publishing several files can still leave a mixed set; fix permissions and rerun sync before deployment. HTTP response bodies and secret values are not printed.

The generated `.secrets.env` format is **literal `KEY=value`**, used with Compose `format: raw`. The wrapper also loads these values directly into the Compose process environment for `${...}` interpolation. **Do not source these files or use `docker compose --env-file .secrets.env`:** that reinterprets quoting and dollar signs. Multiline values must use file delivery. The wrapper rejects expanded `config` output; use `config --quiet`.

For an outage or a fresh restart with known-good caches:

```sh
python3 scripts/infisical-secrets.py compose --host dell --stack traefik --cached -- up -d
python3 scripts/infisical-secrets.py compose --host medion --stack nextcloud --cached -- up -d
```

Cached mode does not contact Infisical or require its machine-identity config. It validates local required inputs and private permissions. This is an explicit operator choice, not a silent fallback after a failed fetch. Docker restart policies already restart existing containers using their existing configuration without contacting Infisical.

## 5. Add an application or a secret

1. Add a folder in the correct host project/environment and populate it with existing or newly provisioned application credentials. Do not use a host's machine identity to write secrets.
2. Add an entry under that host in `shared/infisical/deployments.json`. Set `directory`, `secret_path`, `compose` filenames, and `env_file` if the application accepts environment variables. Add essential variable names to `required`.
3. For file inputs, add `files: {"KEY": "repo/relative/destination"}`. Add the key to `file_only` if it must not also appear in the environment file. File values are always required and must be nonempty. Destinations must remain inside the repository; traversal through symlinks outside it is rejected. Add new generated paths to `.gitignore`.
4. For an env-based service, use a required `env_file` entry with `path: .secrets.env` and `format: raw`. For supported `_FILE` inputs, declare Compose secrets pointing to `.secrets/<filename>` and grant each secret only to the services needing it. Do not also pass the same password via a broad environment file.
5. Add related manifest entries to `dependencies` when one Compose stack consumes more than one folder/file set. Never include Infisical's own bootstrap keys in this mechanism.
6. Run the test suite and Compose validation, sync just that stack, validate with `config --quiet`, and deploy. For new URI-interpolated passwords, respect the application's URI encoding requirements; file delivery avoids that issue where supported.

## 6. Backup, recovery, and rotation

Dell's backup plan now creates a PostgreSQL custom-format dump of Infisical and captures its stack directory, including bootstrap configuration. Live PostgreSQL/Redis files are excluded; Redis is recreated, so pending background jobs are not a recovery guarantee. Keep the dump, the exact Infisical image version, and an **independent offline copy of `ENCRYPTION_KEY` and `AUTH_SECRET`**. Do not change the encryption key as an ad hoc rotation; follow Infisical's supported procedure.

Keep autorestic repository passwords, SSH/backend access, the manager bootstrap file, and the minimal Dell Traefik cache in a recovery kit accessible without Infisical, Pocket ID, or the NAS being healthy. A backup whose only unlock key lives inside Infisical cannot recover Infisical. Host credential files and materialized application secrets are sensitive plaintext at runtime and in restored directories; use encrypted storage/backups and protect Docker/root access.

Restore into an isolated stack using the recorded versions and original bootstrap keys, import the database dump with `pg_restore`, then verify native admin login and a machine fetch before restoring client access. Exercise this procedure after initial setup and periodically thereafter.

To rotate an application credential, update the real upstream service/database and its Infisical value in a coordinated change, synchronize, recreate dependent containers, and verify application and backup access. Updating Infisical alone does not rotate an existing database account or an application-initialization password. Revoke old machine client secrets after replacement has been tested. No unattended password changes or automatic container restarts are configured.

## References

- [Official Docker Compose deployment](https://infisical.com/docs/self-hosting/deployment-options/docker-compose)
- [Infisical server environment variables](https://infisical.com/docs/self-hosting/configuration/envars)
- [Universal Auth login](https://infisical.com/docs/api-reference/endpoints/universal-auth/login)
- [v4 secrets list API](https://infisical.com/docs/api-reference/endpoints/secrets/list)
- [Compose environment file formats](https://docs.docker.com/reference/compose-file/services/#env_file)
- [Compose secret files](https://docs.docker.com/compose/how-tos/use-secrets/)

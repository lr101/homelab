# SOPS and age: setup, migration, and adding deployments

Deployment secrets are encrypted with SOPS and age and committed alongside configuration. Each stack has a JSON object at `secrets/HOST/STACK.enc.json`, referenced by `shared/secrets/deployments.json`. No central secret server, database, machine token, or HTTP API is required.

The deployment wrapper decrypts selected files in memory, validates every selected stack before modifying local files, then writes private runtime files. Existing Compose file mounts and raw environment files are retained. Normal deployment fails if decryption fails; `--cached` explicitly uses existing private files during recovery. No services are automatically restarted by synchronization.

## Initial setup

Requirements: SOPS and age from their official releases, Python 3.9+, and Docker Compose **2.30+** for raw environment files. Tests additionally require the pinned packages in `scripts/requirements.txt`. Install tools on each deployment host; CI needs no production keys.

Generate one age identity per host, **outside the repository**, using a protected directory owned by the deployment account:

```sh
# Example for Dell; repeat on each host with its own identity.
umask 077
mkdir -p ~/.config/homelab
age-keygen -o ~/.config/homelab/dell.agekey
export SOPS_AGE_KEY_FILE="$HOME/.config/homelab/dell.agekey"
```

Create a separate recovery identity offline. Keep its private key in an independent encrypted recovery kit. Copy `.sops.yaml.example` to `.sops.yaml` and replace the placeholder recipients with each host's **public** age recipient and the recovery public recipient. Commit `.sops.yaml`; never commit the private keys. There is intentionally no working recipient configuration until real keys are provisioned.

Each host's encrypted files must list only that host and the recovery recipient. This lets Dell decrypt Dell's secrets without granting access to Medion. SOPS creation rules apply when files are created; after changing recipients run `sops updatekeys FILE` for existing files and review the resulting encrypted diff. Removing a recipient cannot revoke previously decrypted copies or old Git history; rotate affected upstream credentials if a key is compromised.

## Migrate existing values

Migrate one stack at a time. Preserve a protected backup of the current configuration and record deployed image versions. **Keep the actual credentials unchanged during migration**, especially database passwords.

For each stack, generate a skeleton containing its required keys and file inputs:

```sh
python3 scripts/sops-secrets.py template --host dell --stack traefik
mkdir -p secrets/dell
sops secrets/dell/traefik.enc.json
```

Inside the SOPS editor, create a flat JSON object with string values. Copy the skeleton keys and fill in the existing values. Also include other runtime values previously supplied by `.env`; optional nonsecret values may remain in the local `.env`. Store values literally, without dotenv quoting, `KEY=` prefixes, or shell escaping. For multiline JSON/YAML/TOML/INI credentials, store the complete original file text as a JSON string. The SOPS editor encrypts on save; inspect the file before committing to confirm values are `ENC[...]` ciphertext with `sops` metadata.

Do not create plaintext secret JSON under the repository and rely on `.gitignore` to protect it. Do not pipe decrypted values to logs, shell history, or PR comments. The manifest's `encrypted_file` is the authoritative path for each stack, including shared services and file-only entries.

Synchronize, remove migrated passwords/tokens from the old local `.env` into a protected recovery copy, then recreate the containers using the wrapper. Keeping a direct password and its `_FILE` setting together can make an image refuse startup. Verify application behavior and backup hooks before disposing of legacy copies.

## File inputs and existing deployment integrations

| Stack | JSON keys / original content | Result |
| --- | --- | --- |
| All four Traefik stacks | `SECRET_SALT`, `CLIENT_ID`, `CLIENT_SECRET`; `CF_DNS_API_TOKEN` | OIDC environment plus a mounted Cloudflare token file. Use a separate zone-scoped DNS token for each host; replace legacy global API-key auth and revoke those keys after verification |
| Medion / hiring-scraper | Existing `.env` values and `APP_BOOTSTRAP_ENV` containing its complete original text | Existing custom app/database images retain their environment; app receives its bootstrap dotenv file verbatim |
| Medion / Immich | `DB_PASSWORD` | PostgreSQL password file; Immich still receives its existing app environment |
| Medion / Immich kiosk | Original `kiosk.env` values in `secrets/medion/immich-kiosk.enc.json` | `.secrets.kiosk.env`; synchronized as a dependency of `immich-app` |
| Medion / Nextcloud | `NEXTCLOUD_DB_ROOT_PASSWORD`, `NEXTCLOUD_DB_PASSWORD` | File delivery to MariaDB and Nextcloud; root password does not enter the generated application environment |
| Thinkpad / Home Assistant | `MARIADB_ROOT_PASSWORD`, `MARIADB_PASSWORD`; `HOMEASSISTANT_SECRETS_YAML` containing existing `config/secrets.yaml` | Database password files and HA's existing `!secret` configuration. Preserve the real `MARIADB_USER` / `MARIADB_DATABASE` values; defaults are `homeassistant` |
| Thinkpad / PostGIS | `PG_PASSWORD`; keep `PG_USER` available | PostgreSQL password file |
| Medion / logging | `GF_SECURITY_ADMIN_PASSWORD`; `GRAFANA_INI` containing the existing `grafana-config/grafana.ini`; other existing logging environment values | Grafana admin password file and full existing INI, including any configured SSO credentials |
| Thinkpad / MonaServer | `FIREBASE_TOKEN_JSON` containing the existing Firebase credential JSON; existing `.env` values | Private JSON mount plus existing app/RustFS environment |
| Thinkpad / IWI bulletin board | `IWI_SECRET_JSON` containing the existing `secret.json` | Private read-only credential JSON mount |
| Each host / backup-credentials | `AUTORESTIC_ENV` containing the **complete** existing host `.autorestic.env` | The existing host backend-password file, synchronized as a dependency of `shared-backup` |


Other secret-bearing deployments—T3 Code, AdGuard exporters, Pocket ID, tempserver, shared backup/Watchtower/Telegraf, and Synology Telegraf—receive raw `.secrets.env` files. Shared definitions have separate encrypted files per host even though runtime outputs live under `shared/`; use a separate writable checkout on each machine.

Services without deployment-time credentials, such as Jellyfin, Actual Budget, and the homepage, require no artificial secret injection. Application-owned password hashes, sessions, AdGuard state, ACME certificates, SSH credentials, rclone configuration, and registry logins remain protected host/application state and encrypted backups. The manifest consistency test explicitly exempts independently recovered host transport credentials.

## Synchronize and deploy

```sh
# Decrypt and validate all configured entries for this host before publishing.
python3 scripts/sops-secrets.py sync --host medion

# Select one stack; dependencies such as kiosk or backend backup keys are included.
python3 scripts/sops-secrets.py compose --host medion --stack nextcloud -- config --quiet
python3 scripts/sops-secrets.py compose --host medion --stack nextcloud -- up -d
python3 scripts/sops-secrets.py compose --host medion --stack shared-backup -- up -d

# Explicit recovery path: no SOPS binary or key is required if private caches exist.
python3 scripts/sops-secrets.py compose --host dell --stack traefik --cached -- up -d
```

Host-wide synchronization requires every manifest entry to have a populated encrypted file. Use `--stack` for incremental onboarding. Private age keys are selected using SOPS's standard configuration, such as `SOPS_AGE_KEY_FILE`; they are never copied into containers.

Generated `.secrets.env` files contain literal `KEY=value` lines and are consumed with Compose `format: raw`. The wrapper supplies those same values directly to the Compose process environment for `${...}` interpolation. **Do not source these files or use them with `docker compose --env-file`**, which reinterprets quotes and dollar signs. Multiline values must use file delivery. Use `config --quiet`; expanded config output can disclose secrets.

All decryption and value validation happens before publishing. Each file is replaced atomically with mode `0600`; `.secrets` directories use `0700`. A failed decrypt preserves the whole previously materialized set. A filesystem failure during publication can still leave a mixed set; fix permissions and rerun before deployment. Docker/root access can read runtime secrets; SOPS protects Git storage, not a compromised running host.

## Add a deployment or secret

1. Add a manifest entry under the appropriate host with `directory`, `encrypted_file`, `compose` filenames, and an optional runtime `env_file`. Add essential keys to `required`.
2. Map file values with `files: {"KEY": "repo/relative/path"}`; add a key to `file_only` when it must not enter the environment file. File inputs must be nonempty. Add related entries to `dependencies` where needed. Outputs must remain inside the repository.
3. Use `env_file: [{path: .secrets.env, format: raw}]` for env-based apps. Prefer supported `_FILE` settings and grant Compose secrets only to services requiring them. Ensure generated files are ignored by Git.
4. Generate the skeleton with `template`, create/edit the encrypted JSON with SOPS, then run the tests and Compose validation. Sync only the selected stack, validate quietly, deploy, and verify representative behavior.

For URI-interpolated database passwords, follow the application's URI encoding requirements; file delivery avoids that issue where supported. Changing an encrypted value does not rotate an existing database account or initialization password. Coordinate upstream rotation, synchronization, and container recreation.

## Recovery and removal of a previous secret server

Back up host private age identities, the offline recovery identity, encrypted Git files, backend unlock credentials, SSH access, and the minimal Traefik recovery cache independently. A backup whose only decryption key is inside that backup is not recoverable. Test recovery into an isolated checkout using the recorded application image versions and appropriate database dumps.

This repository change removes the former server stack, ingress middleware, and server-specific database backup configuration. No running service or data is deleted by editing the repository. If you previously deployed a secret server, export/migrate its values and verify SOPS deployment first. Then stop its containers using the previous deployment configuration. Retain its encrypted database backup and encryption key for the agreed recovery window; do not remove volumes as part of migration.

## References

- [SOPS documentation](https://getsops.io/docs/)
- [age](https://github.com/FiloSottile/age)
- [Compose secret files](https://docs.docker.com/compose/how-tos/use-secrets/)

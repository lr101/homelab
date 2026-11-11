# Operations and rollout

These files are deployment templates. CI validates Compose structure and hook behavior; it does not connect to hosts, prove firewall rules, or certify a successful restore.

## Roll out this change

Keep an SSH/VPN session open and roll out one host at a time.

1. Preserve the deployed Compose files and record `docker compose images` and image digests before updating. Do not pull floating application images as part of this configuration rollout.
2. Ensure each Traefik `.env` contains a strong `SECRET_SALT`, `CLIENT_ID`, and `CLIENT_SECRET` for its Pocket ID client, plus existing Cloudflare/CrowdSec credentials. Register the appropriate callback URLs in Pocket ID. Compose now rejects missing or empty OIDC values. Immich, hiring-scraper, and Nextcloud also reject missing database credentials and configured data paths. Dynamic files use Go templates; bind-mounted files do not receive Compose substitution.
3. Run `docker compose config --quiet` in the host's Traefik directory (do not print the expanded config into shared logs), then `docker compose up -d`. This creates an internal Docker API network and starts the socket proxy. Only Traefik joins that network; the proxy has no published ports and denies API writes. It still permits container inspection, including metadata; treat it as sensitive.
4. Test HTTPS routing and OIDC in a private browser session. Dell's Traefik dashboard, T3 Code, and `*.dev.dell.lr-projects.de` now require OIDC. Application/API clients that cannot follow an interactive login must use a separately designed authenticated access path; do not remove authentication to work around it. Confirm logout and failed-login behavior before closing the existing session.
5. Recreate shared Watchtower so monitoring-only mode takes effect. Existing opted-in containers are checked and generate email notifications; they are no longer automatically replaced. Preserve the previous notification credentials.
6. Recreate the backup container with the updated hooks mounted and without `privileged`. It retains Docker socket access for database exec hooks, so it still has powerful host access. If a host denies reads due to file permissions or SELinux, configure the specific permission/label required rather than restoring blanket privilege.
7. On Thinkpad, Home Assistant uses host networking and can still connect to `127.0.0.1:3306`; update its recorder connection if it currently uses the host's LAN address. Other hosts can no longer connect directly to this published MariaDB port.

The repository has not pinned existing floating application tags to arbitrary versions: the running versions are unknown. Renovate now proposes digest pins and requires manual merges. Adopt the known running digest first, then upgrade intentionally.

## Backup coverage and prerequisites

The `configured` inventory marker describes a declared source and backend. It does not prove that a backup ran, that every volume is included, or that the data can be restored.

| Host/service | Backup behavior | Required verification |
| --- | --- | --- |
| Medion / Immich | Private PostgreSQL custom-format dump plus configuration and an explicitly mounted upload library | Set `IMMICH_UPLOAD_LOCATION` in the backup `.env` to the **absolute** `UPLOAD_LOCATION` from Immich's `.env`; check that original photos and the dump appear in snapshots |
| Medion / Actual Budget | Stop `budget`, snapshot its bind-mounted data, then restart it; failure cleanup also restarts it | Expect a short nightly outage; test restore of a budget |
| Medion / hiring-scraper | Dump `hiring-scraper-db-1` using its container's database settings; snapshot configuration and dump | Confirm the Compose project/container name matches; Redis queues are deliberately excluded—requeue work after disaster recovery |
| Medion / Nextcloud | Enable maintenance mode, dump MariaDB, snapshot files, disable maintenance mode on success or failure | Verify `NEXTCLOUD_DB_ROOT_PASSWORD` and the generated container names; named database volume is recovered from the SQL dump |
| Thinkpad / HA and application databases | Atomic MariaDB/PostgreSQL dumps and existing service sources | Verify hook container/database names; live raw database directories are not a substitute for the dumps |
| Dell / T3 Code and Traefik | New local + NAS backup plan covers configuration, workspaces, and auth state | Configure a dedicated NAS repository/key and schedule the shared backup container; stop T3 Code during the initial restore drill to assess live-state consistency |
| Medion / historical metrics | Existing logging configuration and Grafana state are copied; Influx data and Prometheus data are excluded | Historical metrics are **not fully covered**. Add an authenticated Influx export and an application-consistent Grafana backup before claiming full recovery |

For Medion, always include the override when starting the shared backup stack:

```sh
cd shared/backup
# .env holds DEVICE_FOLDER_PATH, SHARED_FOLDER_PATH, LOCAL_BACKUP_PATH,
# and IMMICH_UPLOAD_LOCATION. Use absolute host paths.
docker compose -f docker-compose.yml -f compose.medion.yml config --quiet
docker compose -f docker-compose.yml -f compose.medion.yml up -d
```

Other hosts use `docker compose up -d` without the Medion override. Configure the backend passwords in each host's `.autorestic.env`, outside Git. Make a separate, protected copy of recovery credentials. The existing backup README describes backend setup. Ensure the deployed autorestic fork executes `failure` hooks even when a `before` hook fails.

The dump hooks publish a new file only after the database command succeeds. They preserve the last successful file on failure, return a failing status, use mode `0600`, and keep passwords out of logged argv. MariaDB's root password can be supplied as `MARIADB_ROOT_PASSWORD`, `MYSQL_ROOT_PASSWORD`, or `NEXTCLOUD_DB_ROOT_PASSWORD`; simple quoted values are supported. Environment files are not executed as shell scripts.

Local disks and a NAS in the same building do not establish an off-site copy. Verify the NAS's independently managed off-site replication (or add a separate off-site backend), retention, and protection from deletion using host credentials.

## Restore drill (quarterly and after database upgrades)

1. Select a snapshot and restore it to a new, isolated directory. Never restore over a running service.
2. Use the recorded application/database image digests and an isolated Compose project/network without public proxy labels. Restore SQL/custom-format dumps with `mariadb`/`pg_restore`; do not trust copied live database files.
3. Check representative user data: open a photo and its metadata, download a Nextcloud file, open a budget, and query a restored application database.
4. Record date, snapshot ID, image versions, actual recovery time, missing data, and follow-up fixes in a private operations log. Set a recovery-time and acceptable-data-loss target for each important service.
5. Alert on backup failure **and on missing successful snapshots**. A daily backup should alert if no success has occurred within the agreed interval (for example 26 hours). Test delivery by deliberately failing a disposable backup.

## Controlled updates

1. Review the Renovate PR and upstream migration notes; record current digests and take a successful backup.
2. Restore-test database migrations for important services before deployment.
3. Pull and recreate one stack at a time. Update coordinated components such as Immich together, then verify login, health, and representative data.
4. On failure, restore a compatible database snapshot and the recorded images. Reverting an image alone may not undo a schema migration. Prune old images only after the rollback window.

## Local validation

```sh
python3 -m pip install -r scripts/requirements.txt
python3 -m unittest discover -s tests -v
bash scripts/validate-compose.sh
python3 scripts/generate_readme.py thinkpad medion ionos dell
git diff --exit-code README.md
```

`validate-compose.sh` accepts `COMPOSE_BIN=/path/to/standalone-compose`. It uses `--no-interpolate --no-env-resolution`, so it checks all committed models without requiring secrets. Before deployment, also run `docker compose config --quiet` on the target host with its real environment to validate interpolation and mounts.

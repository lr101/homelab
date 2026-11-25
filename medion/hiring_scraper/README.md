# Radius on Medion

Run Compose from the existing `medion/hiring_scraper` deployment directory
with the same project name as the running stack. Check the old app container's
`com.docker.compose.project` label if the project was launched with a custom
name. Keep the existing `./hiring_db` PostgreSQL data, `./radius_codex` private
Codex state, and external `frontend` network. Traefik continues to route
`https://jobs.medion.lr-projects.de` to `app:8000`.

The existing local `.env` remains the service env file and is mounted read-only
as `/run/app-bootstrap.env` in the app containers. Keep it at mode `0600` with
an owner other than the container runtime UID 10001. Preserve the existing
`POSTGRES_PASSWORD`, `HIRING_DB_PASSWORD`, and other settings. The migration
uses the PostgreSQL administrator and ensures the `hiring_app` role exists.
The API and workers run with the application role. Keep `.env` and the two
data directories out of Git.

## Image publication gate

[Upstream PR #146](https://github.com/lr101/hiring-scraper/pull/146) adds
`0040_application_preparation` after `0039_durable_ai_matching_queue`.
The merge commit's CI failed. The published successor
`1e41d40e83e0c8967eac610a94f1732b6854c6b2` contains PR #146 and passed
[upstream CI](https://github.com/lr101/hiring-scraper/actions/runs/44893099509).
Both GHCR manifests and their image configuration labels were independently
verified for Linux amd64. The database image uses PostgreSQL 17, PostGIS 3.5
and pgvector 0.8.7. Before rollout, confirm the Medion host architecture
and the running PostgreSQL major version and installed extensions match the
new image's compatibility requirements.

Add these exact digest references to the existing local `.env`:

```dotenv
HIRING_APP_IMAGE=ghcr.io/lr101/hiring-scraper/hiring-scraper@sha256:122a55a0923322a87a63790ab21efe566a663c283f94ab1a96efff360021fe69
HIRING_DB_IMAGE=ghcr.io/lr101/hiring-scraper/hiring-scraper@sha256:9a75f6ca0f0654af3e2111acfbe8a00da80df478b8babb687f0275a170f4fbc7
```

Pull both digest references before starting downtime. To verify the image
provenance on the host, run
`docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' IMAGE`
for each pulled digest reference and check that both report the revision
above. If a pull or provenance check fails, keep the current deployment running.

## Rollout

1. Save the pre-change Compose file and protected `.env` outside the checkout.
   Record the exact running app and database image IDs or digest references
   and ensure both images are available for rollback. If recording local image
   IDs, give those images dedicated local rollback tags and record those tags.
   Check for in-flight preparations before scheduling downtime. Confirm the
   existing database reports PostgreSQL 17 and Alembic revision
   `0039_durable_ai_matching_queue`. Review the installed extension versions.

   ```sh
   docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U postgres -d hiring \
     -c 'SHOW server_version;' \
     -c 'SELECT extname, extversion FROM pg_extension ORDER BY extname;' \
     -c 'SELECT version_num FROM alembic_version;'
   ```
2. Stop the old `app` first so all bundled workers stop. Cleanly stop `db`,
   then take a consistent offline backup of both `./hiring_db` and
   `./radius_codex` with ownership and modes intact. Keep both stopped until
   the backup completes. Retain the old Compose config and image references
   with the backup.
3. Install the new Compose config and bundled `docker/` files into the same
   deployment directory. The seccomp profile comes from the upstream merge.
   On Ubuntu 24.04+, load the bundled AppArmor profile with
   `sudo install -m 0644 docker/chromium.apparmor /etc/apparmor.d/hiring-scraper-chromium`
   and `sudo apparmor_parser -r /etc/apparmor.d/hiring-scraper-chromium`, then set
   `HIRING_APPARMOR_PROFILE=hiring-scraper-chromium` in `.env`. The Compose
   default for the AppArmor setting matches upstream.
4. With the same project name and verified image references, run the following
   in order. Stop on any failure.

   ```sh
   docker compose config --quiet
   docker compose up -d --wait db
   docker compose up --no-deps --force-recreate --exit-code-from migrate migrate
   docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U postgres -d hiring \
     -c 'SELECT version_num FROM alembic_version;'
   ```

   Require exit code 0 from `migrate` and confirm the database revision is
   `0040_application_preparation` for this release. The migration creates
   `application_preparation_tasks` for durable preparation state and leases.
   Compose also gates normal startup of the API and workers on a successful
   migration. After explicitly verifying the one-shot run above, start the
   runtime services without rerunning it:

   ```sh
   docker compose up -d --no-deps --wait app worker discovery-worker \
     search-snapshot-worker profile-match-worker semantic-search-worker \
     application-preparation-worker
   docker compose ps -a
   curl -fsS https://jobs.medion.lr-projects.de/health/ready
   ```

   The worker start command assumes the existing `RUN_BACKGROUND_WORKERS`
   setting is enabled (the upstream default). With it set to `false`, start
   only `app`; worker commands exit successfully by design. Check worker logs
   for crash loops and confirm a preparation job progresses to completion
   through the UI.

## Rollback

If startup or verification fails, stop the new app and workers before
investigating; a stopped API alone leaves workers able to write. For a
rollback to the old bundled app, use the new Compose configuration to stop
the whole project with `docker compose stop`, then remove its stopped
service containers with `docker compose rm -f`. Keep the verified image settings available until
those commands finish.

Restore both pre-rollout directory backups while PostgreSQL and all app
processes are stopped. Preserve the failed rollout's data separately before
restoring if it is needed for recovery. Restoration discards database and
Codex-state changes since the backup, including new preparation jobs and
drafts. The old image may reject an Alembic revision it does not recognize,
so restore the pre-migration database before starting it.

Put the previous Compose file and protected env back in the same deployment
directory, with the old app and db image references pinned to the recorded
images. Run `docker compose up -d --pull never --wait db app` with the original
project name, then verify the old database revision and the public readiness
endpoint.

## Job analysis models

Radius matches jobs with GPT-6 Luna through the connected ChatGPT subscription while
it has headroom (five-hour window below 60%, week below 70%), and with two local
models otherwise; Radius Settings -> Matching model switches to **Local only**. The
`job-analysis-worker` therefore mounts `./radius_codex` like the app (upstream
`docs/local-models.md`). The local models:
`llm` (Qwen3.5-4B, about 4.5 GB RAM) and `llm-reranker` (Qwen3-Reranker-0.6B, about
1.5 GB RAM), both llama.cpp servers on CPU. They share four cores at low CPU
priority (`cpu_shares: 256`), so Immich, Jellyfin and Nextcloud keep precedence;
the `job-analysis-worker` calls them one at a time. Override the limits with
`LLM_THREADS`, `LLM_MEMORY_LIMIT` and `LLM_RERANKER_MEMORY_LIMIT` in `.env`.

The first start downloads about 3.4 GB of pinned weights into the `llm_models`
named volume (not part of the `./hiring_scraper` backup; it is re-downloadable).
Jobs stay "Analysing…" and out of Journey until the models have read them; jobs in
the profile's search area go first, at roughly one posting per minute on four
cores. Requires the Radius image with migration `0044_local_job_analysis`.

Check progress:

```sh
docker compose logs -f job-analysis-worker
docker compose exec -T db psql -U postgres -d hiring \
  -c 'SELECT status, count(*) FROM job_analyses GROUP BY status;' \
  -c 'SELECT count(*) AS queued FROM job_analysis_queue;' \
  -c 'SELECT count(*) AS evidence_queued FROM match_evidence_queue;'
```

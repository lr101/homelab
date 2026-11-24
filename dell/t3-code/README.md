# T3 Code with a sandboxed browser

The entrypoint starts the local PostgreSQL cluster as root, then switches to
`node` (UID/GID 1000) before running bootstrap, GitHub authentication, T3, and
agents. Chrome's sandbox stays enabled. The image includes Chrome's shared
libraries and fonts; T3 downloads its own matching browser on first use.

`T3CODE_NO_BROWSER=true` only suppresses automatically opening the T3 UI at
startup. It does not disable agent browser tabs. The Compose service explicitly
sets `T3CODE_SERVER_BROWSER_SANDBOX=1`; the entrypoint rejects `0`.

## Migrate an existing deployment

Back up the persisted directories and stop the service before changing ownership.
Run these commands from this directory on the Docker host, after pulling this
change:

```sh
docker compose stop t3-code
sudo mkdir -p workspace t3-data codex-data codex-data-eli mise-data
sudo chown -R 1000:1000 workspace t3-data codex-data codex-data-eli mise-data
docker compose up -d --build t3-code
```

The bind-mount source directories are unchanged, so existing sessions, worktrees,
and logins remain on disk. Their container destinations now use `/home/node`.
Compatibility symlinks from `/root/.t3`, `/root/.codex`, `/root/.codex_p`, and
`/root/.agents` preserve previously saved absolute paths. `/root` permits traversal
but not directory listing by other users. New processes use `/home/node` as HOME.
Remove stale `/root` overrides for HOME, CODEX_HOME, or T3CODE_HOME in `.env` or
custom bootstrap scripts, and make any custom paths writable by UID 1000.

If `/workspace/bootstrap-codex.sh` exists, it must be executable and work as
`node`. Move any root-only package installation into the Dockerfile. Git identity
and the credential helper are configured system-wide so they apply to `node`.
The startup script reports unwritable persisted directories instead of recursively
changing workspace ownership on every restart. The skills mount only needs to be
readable by `node`.

The bundled preview database helper can still administer local development
PostgreSQL through `sudo -n -u postgres -- psql`. The sudo rule permits only
`/usr/bin/psql` as `postgres`, not commands as root. This is database superuser
access (including PostgreSQL's ability to execute commands as the postgres OS
user); the development database is not isolated from agents. Root hosts retain
the helper's existing `runuser` behavior.

## Docker and host sandbox requirements

`seccomp-browser.json` is vendored from Docker's default profile at
[moby/profiles commit 9ee3204](https://github.com/moby/profiles/blob/9ee3204f0d0cfa19ca2c6a7b0fcf5a2b353954ae/seccomp/default.json),
under the adjacent Apache-2.0 license. The only change adds an allow rule for
`clone`, `setns`, and `unshare`, following
[Playwright's sandboxed Chromium guidance](https://playwright.dev/docs/docker#crawling-and-scraping).
Other syscall filtering remains, including the `clone3` ENOSYS fallback needed
by modern libc. The service does not use privileged mode, SYS_ADMIN,
seccomp-unconfined, or host IPC. A private 1 GiB `/dev/shm` supports Chromium.

The Docker host must allow unprivileged user namespaces. If Chrome reports
`Operation not permitted` or `No usable sandbox`, check the host's AppArmor policy
and namespace limits as well as seccomp. On Ubuntu, an administrator may need a
container AppArmor profile that allows user namespaces while retaining the
usual Docker rules. Running `t3 browser setup` inside a container cannot change
the host's container security policy. Do not disable AppArmor globally or turn
off Chrome's sandbox to work around a denied namespace operation. See
[T3's browser host setup guide](https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md#browser-host-setup).

## Verify after deployment

```sh
# The server and agents must run as node, although initialization used root.
docker compose exec t3-code ps -eo user,pid,ppid,args
# Diagnose installed browser libraries/fonts and host sandbox restrictions.
docker compose exec --user node t3-code t3 browser setup
```

Open a browser tab in T3 and navigate to a page. Confirm it renders, then inspect
the Chrome command line in the process listing: it must not contain `--no-sandbox`.
The first tab needs network access to download Chrome. The diagnostic alone does
not prove Docker permits a browser launch; verify an actual tab on the target
host. These settings protect Chrome's browser processes; agent permission modes
and filesystem sandboxing are configured separately in T3.

## Local checks

On Linux with root/chroot access, run from the repository root:

```sh
python3 -m unittest discover -s dell/t3-code/tests -v
sh -n dell/t3-code/entrypoint.sh
bash -n dell/t3-code/skills/serve-dev-worktree/scripts/prepare_monaserver_preview_db.sh
```

The startup tests use a disposable chroot and real UID changes. PostgreSQL and
GitHub CLI are stand-ins; they do not prove a Docker build or Chrome launch.

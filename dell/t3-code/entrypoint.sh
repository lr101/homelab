#!/bin/sh
set -eu

# Chrome's sandbox cannot run as root. Only initialize the system service here;
# bootstrap scripts, credentials, T3, and its agents run as node below.
if [ "$(id -u)" = 0 ]; then
    if ! pg_isready -q -h 127.0.0.1 -p 5432; then
        cluster="$(pg_lsclusters --no-header | awk 'NR == 1 { print $1 ":" $2 }')"
        version="${cluster%%:*}"
        name="${cluster#*:}"

        if [ -z "$version" ] || [ "$version" = "$name" ]; then
            echo "No PostgreSQL cluster found" >&2
            exit 1
        fi

        pg_ctlcluster --skip-systemctl-redirect "$version" "$name" start
        pg_isready -h 127.0.0.1 -p 5432
    fi
    exec setpriv --reuid=node --regid=node --init-groups \
        env HOME=/home/node USER=node LOGNAME=node "$0" "$@"
fi

if [ "${T3CODE_SERVER_BROWSER_SANDBOX:-1}" = 0 ]; then
    echo "Chrome sandboxing must remain enabled; remove T3CODE_SERVER_BROWSER_SANDBOX=0" >&2
    exit 1
fi

# setpriv runs T3 as node with HOME=/home/node. Keep persisted state and tools writable by
# that user; see README.md for the one-time bind-mount migration.
for directory in "$HOME/.t3" "$HOME/.codex" "$HOME/.codex_p" /workspace /opt/mise; do
    if [ ! -w "$directory" ]; then
        echo "$directory must be writable by $(id -un); follow the README migration" >&2
        exit 1
    fi
done

if [ -f /workspace/bootstrap-codex.sh ]; then
    /workspace/bootstrap-codex.sh
fi
gh auth setup-git

exec "$@"

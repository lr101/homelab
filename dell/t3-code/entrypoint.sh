#!/bin/sh
echo "1. Container starting up..."

# Place your setup tasks here (e.g., git pull, env checks)
echo "2. Running background initialization..."
/workspace/bootstrap-codex.sh
gh auth setup-git

# Launching postgres
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


# This executes the CMD command passed from the Dockerfile
echo "3. Starting main application..."
exec "$@"

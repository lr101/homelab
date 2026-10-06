#!/bin/sh
echo "1. Container starting up..."

# Place your setup tasks here (e.g., git pull, env checks)
echo "2. Running background initialization..."
/workspace/bootstrap-codex.sh
gh auth setup-git

# This executes the CMD command passed from the Dockerfile
echo "3. Starting main application..."
exec "$@"

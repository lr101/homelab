#!/bin/sh

[ "$1" = "get" ] || exit 0

while IFS='=' read -r key value; do
    case "$key" in
        host) host="$value" ;;
    esac
done

[ "$host" = "github.com" ] || exit 0

echo "username=${GITHUB_USER:-x-access-token}"
echo "password=$(cat /run/secrets/github_token)"

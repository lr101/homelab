#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash shared/grype/install.sh" >&2
  exit 1
fi
for executable in docker python3 systemctl; do
  command -v "$executable" >/dev/null || { echo "Missing prerequisite: $executable" >&2; exit 1; }
done
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Read the pinned image from the scanner without executing the scanner.
scanner_image="$(python3 - "$source_dir/scan.py" <<'PY'
import ast
import sys
for statement in ast.parse(open(sys.argv[1]).read()).body:
    if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'IMAGE' for target in statement.targets):
        print(ast.literal_eval(statement.value))
        break
else:
    raise SystemExit('IMAGE pin missing')
PY
)"
docker info >/dev/null
docker pull "$scanner_image"
systemctl stop homelab-grype.timer homelab-grype.service 2>/dev/null || true
install -d -m 0755 /usr/local/lib/homelab-grype /var/lib/homelab-grype /var/lib/homelab-grype/metrics
install -m 0755 "$source_dir/scan.py" /usr/local/lib/homelab-grype/scan.py
install -m 0644 "$source_dir/homelab-grype.service" /etc/systemd/system/homelab-grype.service
install -m 0644 "$source_dir/homelab-grype.timer" /etc/systemd/system/homelab-grype.timer
systemctl daemon-reload
systemctl enable --now homelab-grype.timer
systemctl start --no-block homelab-grype.service
echo "Installed daily Grype scanning and started the first scan."
echo "Follow progress: sudo journalctl -fu homelab-grype.service"
echo "Restart Telegraf to load the updated input: cd shared/telegraf && sudo docker compose up -d --force-recreate"

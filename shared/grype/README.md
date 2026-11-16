# Grype vulnerability monitoring

Run the same setup on each Fedora or Debian server where this repository is cloned.
Docker runs a pinned Grype image; a host systemd timer scans daily at 04:00 in the
server's timezone, with up to 30 minutes of jitter and catch-up after downtime.
No native Grype package or Python dependencies are required.

## Install on each server

Prerequisites: rootful Docker Engine, systemd, Python 3.9+, and outbound access to
Docker Hub and Grype's vulnerability database. This setup uses `/var/run/docker.sock`;
rootless Docker and Podman are not configured.

From the repository root:

```sh
sudo bash shared/grype/install.sh
sudo journalctl -fu homelab-grype.service
```

The installer pulls the pinned image, copies the scanner into
`/usr/local/lib/homelab-grype`, enables the timer, and starts the first scan in the
background. It does not install OS packages or change running application containers.
Re-run it after pulling scanner changes from Git; installed copies do not depend
on the clone's location. Reinstalling replaces these two systemd units.

Load the new Telegraf input on that server (using its existing `.env`):

```sh
cd shared/telegraf
sudo docker compose up -d --force-recreate
sudo docker compose logs --tail=50 telegraf
```

Telegraf reads `/var/lib/homelab-grype/metrics/metrics.lp` through a dedicated read-only
mount and sends it to the configured InfluxDB bucket. The mount's `z` option shares
the SELinux label on just the public metrics directory for Fedora. No new tokens or ports are
needed. Hosts without Grype installed can continue using the shared Telegraf config.

## What is scanned

- Host: `dir:/host`, with Fedora/Debian OS identification required for success.
  Virtual filesystems, home/root directories, temporary/removable mounts, Docker/
  Podman storage, and the scanner's own state are excluded. This scans installed
  package databases and software in the remaining filesystem; large application
  data directories can increase scan time. Software in excluded paths is not covered.
- Containers: all running Docker containers, including Compose services from this
  repository and standalone containers. The immutable local image ID is scanned,
  once per image per run. Private images already pulled locally need no registry
  credentials. Stopped containers, writable container layers, and mounted application
  data are not scanned. This is image vulnerability detection, not intrusion detection.
- Add `homelab.grype.scan: "false"` to a container's labels to exclude it.

The host and Docker socket are mounted read-only. Socket access still grants Docker
API privileges. The scanner runs as root with dropped capabilities and no extra
privileges. SELinux labeling is disabled **only for the short-lived Grype container**
so Fedora host files and the socket can be read without relabeling `/`.

## Reports and metrics

State lives in `/var/lib/homelab-grype`. `reports/<source-sha256>.json` retains the
latest successful detailed report per active image/host; `state.json` maps targets
to sources. Reports and state are root-readable. Retired image reports are removed
after successful container discovery. `metrics/metrics.lp` is world-readable summary data.

The InfluxDB measurement is `grype_scan`:

| Tags | Meaning |
| --- | --- |
| `server` | Actual host hostname, independent of Telegraf's container hostname |
| `kind` | `host`, `container`, or `collector` |
| `target` | `host`, container name, or `discovery` |
| `project`, `service` | Compose labels, or standalone/host/collector values |

| Integer fields | Meaning |
| --- | --- |
| `critical`, `high`, `medium`, `low`, `negligible`, `unknown` | Vulnerability/package pairs by severity |
| `total`, `fixable` | Total pairs and pairs with a known fix |
| `success` | 1 if the latest attempt succeeded, otherwise 0 |
| `last_attempt`, `last_success` | Unix seconds; last_success is 0 until the first success |

Duplicate matches for the same vulnerability ID and artifact ID are counted once.
These are package findings, not unique CVEs across the entire server. Vulnerabilities
with no fix remain included. Grype's database age/hash validation remains enabled,
and update-check failures cause scan failure rather than silently using cached data.

Telegraf republishes the snapshot every minute with a fresh metric timestamp.
**Use `last_success` to detect stale scans**, rather than the Influx point timestamp.
Failed scans preserve prior counts for that same source but set `success=0`; first-time
failures omit counts. A changed image does not inherit the previous image's counts.
Targets disappear from new snapshots when their containers stop or are excluded.

## Grafana

Import [grafana-dashboard.json](grafana-dashboard.json), select your InfluxDB **Flux**
data source, and set the bucket variable to your Telegraf bucket. It contains host
and container finding tables, plus collector health. Tables use the most recent
five minutes of snapshots so retired containers disappear promptly. Counts shown
on failed rows are previous results: always inspect success and scan age.

Suggested alerts: critical or high findings with `success=1`; latest `success=0`;
`last_success=0` or scan age greater than 36 hours; and absent metrics for an expected
server. A stopped timer or Telegraf must not be interpreted as a clean scan.

## Operations

```sh
sudo systemctl start homelab-grype.service  # Run now (waits for completion)
systemctl list-timers homelab-grype.timer
sudo systemctl status homelab-grype.service
sudo cat /var/lib/homelab-grype/metrics/metrics.lp
sudo journalctl -u homelab-grype.service --since yesterday
```

To change the schedule, use `sudo systemctl edit homelab-grype.timer` and override
`OnCalendar` (clear it first with an empty `OnCalendar=`). To set a stable server name
or a longer timeout, override the service's `ExecStart` (clear it first), retaining
the scanner path and adding `--server medion` or `--timeout 3600`. Restart the timer
after edits. Finding vulnerabilities does not fail the service; scan/discovery errors do.

Change the `IMAGE` pin in `scan.py` and re-run the installer to upgrade Grype.
An image digest can be used instead of the version tag. Scan processes time out
after 30 minutes per source and clean up their scan container; concurrent scanner
runs are rejected by a file lock.

To disable scans: `sudo systemctl disable --now homelab-grype.timer`. Let an active
scan finish before uninstalling/reinstalling. To uninstall, stop the timer, remove
the installed service/timer and scanner files, then run `systemctl daemon-reload`.
Remove `/var/lib/homelab-grype` separately if you no longer want reports/metrics.

## Verification and upstream references

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s shared/grype/tests -v
bash -n shared/grype/install.sh
```

[Grype targets](https://oss.anchore.com/docs/guides/vulnerability/scan-targets/),
[supported operating systems](https://oss.anchore.com/docs/capabilities/all-os/),
[configuration](https://oss.anchore.com/docs/reference/grype/configuration/),
[Telegraf file input](https://docs.influxdata.com/telegraf/v1/input-plugins/file/).

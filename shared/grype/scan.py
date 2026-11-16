#!/usr/bin/env python3
"""Scan the local host and deployed Docker images; publish Telegraf metrics."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import time

IMAGE = "anchore/grype:v0.120.0"
SEVERITIES = ("critical", "high", "medium", "low", "negligible", "unknown")
HOST_EXCLUDES = (
    "proc", "sys", "dev", "run", "tmp", "mnt", "media", "home", "root",
    "var/lib/docker", "var/lib/containers", "var/lib/homelab-grype",
)


def atomic_write(path, contents, mode=0o600):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def summarize(report):
    """Count unique vulnerability/package pairs, avoiding duplicate match paths."""
    if not isinstance(report.get("matches"), list):
        raise ValueError("Grype report has no matches array")
    counts = {severity: 0 for severity in SEVERITIES}
    seen = set()
    fixable = 0
    for match in report["matches"]:
        vuln, artifact = match["vulnerability"], match["artifact"]
        key = (vuln["id"], artifact["id"])
        if key in seen:
            continue
        seen.add(key)
        severity = vuln.get("severity", "unknown").lower()
        counts[severity if severity in counts else "unknown"] += 1
        fixable += vuln.get("fix", {}).get("state") == "fixed"
    return {**counts, "total": len(seen), "fixable": fixable}


def escape_tag(value):
    return str(value).replace("\\", "\\\\").replace("\n", "_").replace("\r", "_").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def metrics(state, server):
    lines = []
    for target in state.values():
        tags = {"server": server, **target["tags"]}
        prefix = "grype_scan," + ",".join(f"{key}={escape_tag(value)}" for key, value in tags.items())
        fields = {"success": target["success"], "last_attempt": target["last_attempt"],
                  "last_success": target.get("last_success", 0)}
        fields.update(target.get("counts", {}))
        lines.append(prefix + " " + ",".join(f"{key}={value}i" for key, value in fields.items()))
    return "\n".join(lines) + "\n"


def docker_json(arguments):
    result = subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True, timeout=60)
    return json.loads(result.stdout)


def discover():
    ids = subprocess.run(["docker", "ps", "-q"], check=True, capture_output=True,
                         text=True, timeout=60).stdout.split()
    # Inspect in batches to avoid command-line limits. Scan identical images once.
    containers = []
    for offset in range(0, len(ids), 100):
        containers.extend(docker_json(["inspect", *ids[offset:offset + 100]]))
    targets = []
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        if labels.get("homelab.grype.scan", "true").lower() == "false":
            continue
        name = container["Name"].lstrip("/")
        targets.append(("container:" + name, "docker:" + container["Image"], {
            "kind": "container", "target": name,
            "project": labels.get("com.docker.compose.project", "standalone"),
            "service": labels.get("com.docker.compose.service", name),
        }))
    return targets


def run_scan(source, args, report_path):
    name = "homelab-grype-" + hashlib.sha256(source.encode()).hexdigest()[:16]
    command = ["docker", "run", "--rm", "--name", name, "--pull=never",
               "--user", "0:0", "--security-opt", "label=disable", "--cap-drop=ALL",
               "--security-opt", "no-new-privileges", "--read-only",
               "--tmpfs", "/tmp:rw,nosuid,nodev",
               "--mount", f"type=bind,src={args.state_dir / 'cache'},dst=/cache",
               "--env", "GRYPE_DB_CACHE_DIR=/cache/db",
               "--env", "GRYPE_DB_REQUIRE_UPDATE_CHECK=true",
               "--env", "GRYPE_CHECK_FOR_APP_UPDATE=false"]
    if source.startswith("dir:"):
        command += ["--mount", "type=bind,src=/,dst=/host,readonly,bind-propagation=rprivate"]
    else:
        command += ["--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly"]
    command += [args.image, source, "-o", "json"]
    if source.startswith("dir:"):
        for directory in HOST_EXCLUDES:
            command += ["--exclude", f"./{directory}/**"]
    with tempfile.TemporaryFile(mode="w+") as output:
        try:
            subprocess.run(command, stdout=output, check=True, timeout=args.timeout)
            output.seek(0)
            report = json.load(output)
            counts = summarize(report)
            if source.startswith("dir:") and report.get("distro", {}).get("name") not in ("fedora", "debian"):
                raise ValueError("Host scan did not identify Fedora or Debian")
            atomic_write(report_path, json.dumps(report))
            return counts
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # Killing docker's client does not stop the scan container.
            subprocess.run(["docker", "rm", "-f", name], timeout=60, check=False)
            raise


def scan(args):
    state_path = args.state_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    previous_discovery = state.get("discovery", {})
    server = args.server or socket.gethostname()
    failures = 0

    def publish():
        atomic_write(state_path, json.dumps(state))
        atomic_write(args.state_dir / "metrics" / "metrics.lp", metrics(state, server), 0o644)

    targets = [("host", "dir:/host", {"kind": "host", "target": "host", "project": "host", "service": "host"})]
    try:
        targets += discover()
        active = {key for key, _, _ in targets}
        state = {key: value for key, value in state.items() if key in active}
        discovery_ok = 1
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        print(f"Container discovery failed: {error}", file=sys.stderr)
        failures += 1
        discovery_ok = 0
        # Keep previous targets, but mark their data as unavailable.
        for key, value in state.items():
            if key.startswith("container:"):
                value.update(success=0, last_attempt=int(time.time()))
    state["discovery"] = {"tags": {"kind": "collector", "target": "discovery", "project": "collector", "service": "collector"},
                          "success": discovery_ok, "last_attempt": int(time.time()),
                          "last_success": previous_discovery.get("last_success", 0)}
    if discovery_ok:
        state["discovery"]["last_success"] = int(time.time())
    results = {}
    for key, source, tags in targets:
        previous = state.get(key, {})
        # Do not retain counts from an old image after a container is recreated.
        if previous.get("source") != source:
            previous = {}
        target = {**previous, "source": source, "tags": tags, "success": 0, "last_attempt": int(time.time())}
        state[key] = target
        publish()
        print(f"Scanning {key} ({source})", flush=True)
        try:
            if source not in results:
                report_path = args.state_dir / "reports" / (hashlib.sha256(source.encode()).hexdigest() + ".json")
                try:
                    results[source] = run_scan(source, args, report_path)
                except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as error:
                    results[source] = error
            result = results[source]
            if isinstance(result, Exception):
                raise result
            target.update(counts=result, success=1, last_success=int(time.time()))
            print(f"Completed {key}: {result['total']} findings", flush=True)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as error:
            print(f"Scan failed for {key}: {error}", file=sys.stderr)
            failures += 1
        publish()
    if discovery_ok:
        active_reports = {hashlib.sha256(value["source"].encode()).hexdigest() + ".json"
                          for value in state.values() if "source" in value}
        for report in (args.state_dir / "reports").glob("*.json"):
            if report.name not in active_reports:
                report.unlink()
    return int(failures > 0)


def main():
    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/homelab-grype"))
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--server", help="Stable server name; defaults to the host hostname")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout per scan in seconds")
    args = parser.parse_args()
    args.state_dir = args.state_dir.resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    (args.state_dir / "metrics").mkdir(exist_ok=True, mode=0o755)
    for directory in ("cache", "reports"):
        (args.state_dir / directory).mkdir(exist_ok=True, mode=0o700)
    with (args.state_dir / "scan.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A scan is already running", file=sys.stderr)
            return 1
        return scan(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

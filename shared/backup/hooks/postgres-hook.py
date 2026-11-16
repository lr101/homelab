#!/usr/bin/env python3
"""postgres-hook.py

Usage: postgres-hook.py CONTAINER PG_USER DB1 [DB2 ...]

Creates compressed pg_dump backups by exec'ing into a running postgres docker container.
"""

from __future__ import annotations

import argparse

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from backup_dump import atomic_dump


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create pg_dump backups from a Docker Postgres container")
    p.add_argument("container", help="Docker container name or id running Postgres")
    p.add_argument("pg_user", help="Postgres user")
    p.add_argument("dbs", nargs="+", help="Database names (must match num_dbs)")
    p.add_argument("--backup-dir", default=str(Path.cwd() / "postgres-backups"), help="Directory to write dumps to")
    return p.parse_args()


def check_docker_present() -> None:
    if shutil.which("docker") is None:
        print("ERROR: docker CLI not found in PATH", file=sys.stderr)
        sys.exit(2)


def container_running(container: str) -> bool:
    # check names first, then ids
    try:
        out = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"], text=True)
        if any(line.strip() == container for line in out.splitlines()):
            return True
        out = subprocess.check_output(["docker", "ps", "--format", "{{.ID}}"], text=True)
        if any(line.strip() == container for line in out.splitlines()):
            return True
    except subprocess.CalledProcessError:
        pass
    return False



def dump_db(container: str, pg_user: str, db: str, out_path: Path) -> int:
    cmd = ["docker", "exec", "-u", pg_user, container, "pg_dump", "-U", pg_user, "-Fc", db]
    print(f"Running: {' '.join(shlex.quote(c) for c in cmd)}")
    return atomic_dump(cmd, out_path)


def main() -> int:
    args = parse_args()

    check_docker_present()

    if not container_running(args.container):
        print(f"ERROR: container '{args.container}' is not running (by name or id)", file=sys.stderr)
        return 3

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0

    for db in args.dbs:
        out_file = backup_dir / f"{args.container}-{db}.dump"
        rc = dump_db(args.container, args.pg_user, db, out_file)
        if rc != 0:
            exit_code = rc

    if exit_code == 0:
        print("All dumps completed successfully")
    else:
        print(f"Completed with errors (exit {exit_code})", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

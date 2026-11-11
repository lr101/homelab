#!/usr/bin/env python3
"""mariadb-hook.py

Usage: mariadb-hook.py CONTAINER DBS [DBS ...]

Creates mariadb-dump backups by exec'ing into a running MariaDB/MySQL docker container.
Reads root password from .env file in current directory.
"""

from __future__ import annotations

import argparse
import os

import shutil
import subprocess
import sys
from pathlib import Path

from backup_dump import atomic_dump


def load_env(env_path: Path = Path.cwd() / ".env") -> dict[str, str]:
    """Load environment variables from .env file."""
    env_vars = {}
    if not env_path.exists():
        print(f"WARNING: .env file not found at {env_path}", file=sys.stderr)
        return env_vars
    
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                env_vars[key.strip()] = value
    
    return env_vars


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create mariadb-dump backups from a Docker MariaDB/MySQL container")
    p.add_argument("container", help="Docker container name or id running MariaDB/MySQL")
    p.add_argument("dbs", nargs="+", help="Database names to dump")
    p.add_argument("--folder", help="Folder containing .env and .backup subdirectory (constructs --env-file and --backup-dir)")
    p.add_argument("--env-file", help="Path to .env file (default: <folder>/.env or .env in current directory)")
    p.add_argument("--backup-dir", help="Directory to write dumps to (default: <folder>/.backup or ./mariadb-backups)")
    p.add_argument("--mysql-user", default="root", help="MySQL user (default: root)")
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


def dump_db(container: str, mysql_user: str, mysql_password: str, db: str, out_path: Path) -> int:
    # Docker reads the value from its own environment; never put it in argv/logs.
    cmd = ["docker", "exec", "-e", "MYSQL_PWD", container, "mariadb-dump",
           "-u", mysql_user, "--single-transaction", "--routines", "--events", "--triggers", db]
    print(f"Dumping MariaDB database {db!r} from {container!r}")
    return atomic_dump(cmd, out_path, env={**os.environ, "MYSQL_PWD": mysql_password})


def main() -> int:
    args = parse_args()

    check_docker_present()

    # Construct env_file and backup_dir from --folder if provided
    if args.folder:
        folder_path = Path(args.folder)
        env_file = args.env_file or str(folder_path / ".env")
        backup_dir_str = args.backup_dir or str(folder_path / ".backup")
    else:
        env_file = args.env_file or str(Path.cwd() / ".env")
        backup_dir_str = args.backup_dir or str(Path.cwd() / "mariadb-backups")

    # Load .env file to get password
    env_vars = load_env(Path(env_file))
    mysql_password = (env_vars.get("MARIADB_ROOT_PASSWORD") or env_vars.get("MYSQL_ROOT_PASSWORD")
                      or env_vars.get("NEXTCLOUD_DB_ROOT_PASSWORD") or env_vars.get("MARIADB_PASSWORD"))
    
    if not mysql_password:
        print(f"ERROR: Database root password not found in {env_file}", file=sys.stderr)
        return 2

    if not container_running(args.container):
        print(f"ERROR: container '{args.container}' is not running (by name or id)", file=sys.stderr)
        return 3

    backup_dir = Path(backup_dir_str)
    backup_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0

    for db in args.dbs:
        out_file = backup_dir / f"{args.container}-{db}.bak"
        print(f"Dumping database '{db}' to '{out_file}'")
        rc = dump_db(args.container, args.mysql_user, mysql_password, db, out_file)
        if rc != 0:
            print(f"ERROR: dump failed for database '{db}'", file=sys.stderr)
            exit_code = rc
        else:
            print(f"Success: wrote {out_file}")

    if exit_code == 0:
        print("All dumps completed successfully")
    else:
        print(f"Completed with errors (exit {exit_code})", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

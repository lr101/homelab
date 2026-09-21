#!/usr/bin/env python3
"""
Delayed package updates for Debian and Fedora.

Policy:
  - Security updates are installed automatically ASAP using the
    distribution's native automatic-update mechanism.
  - Other updates must remain the exact candidate version for 96 hours
    before they are installed.
  - If the candidate version changes, the 96-hour timer starts again.

Supported:
  - Debian
  - Fedora with DNF5

Initial installation:
    sudo python3 setup-delayed-upgrades.py

Afterwards:
    sudo delayed-upgrades status
    sudo delayed-upgrades check
    sudo delayed-upgrades run

State:
    /var/lib/delayed-upgrades/state.json

Logs:
    journalctl -u delayed-upgrades.service
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


DELAY_HOURS = 96

DEST = Path("/usr/local/sbin/delayed-upgrades")

STATE_DIR = Path("/var/lib/delayed-upgrades")
STATE_FILE = STATE_DIR / "state.json"

SERVICE_FILE = Path(
    "/etc/systemd/system/delayed-upgrades.service"
)

TIMER_FILE = Path(
    "/etc/systemd/system/delayed-upgrades.timer"
)


# ------------------------------------------------------------
# General helpers
# ------------------------------------------------------------

def run(args, capture=False, allowed=(0,)):
    print("+", " ".join(args), flush=True)

    env = os.environ.copy()
    env["LC_ALL"] = "C"

    result = subprocess.run(
        args,
        text=True,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )

    if result.returncode not in allowed:
        stdout = result.stdout or ""
        stderr = result.stderr or ""

        raise RuntimeError(
            f"Command failed ({result.returncode}): "
            f"{' '.join(args)}\n"
            f"{stdout}\n{stderr}"
        )

    if capture:
        return result.stdout

    return ""


def require_root():
    if os.geteuid() != 0:
        sys.exit(
            "This command must be run as root.\n"
            "Use: sudo python3 setup-delayed-upgrades.py"
        )


def atomic_write(path, contents, mode=0o644):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
    )

    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(contents)

        os.chmod(temporary, mode)
        os.replace(temporary, path)

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def get_distribution():
    data = {}

    with open("/etc/os-release") as handle:
        for line in handle:
            line = line.strip()

            if "=" not in line:
                continue

            key, value = line.split("=", 1)

            data[key] = value.strip('"')

    distro = data.get("ID", "").lower()

    if distro not in ("debian", "fedora"):
        sys.exit(
            f"Unsupported distribution: {distro}\n"
            "Supported distributions: Debian, Fedora"
        )

    return distro


# ------------------------------------------------------------
# State database
# ------------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {
            "versions": {},
        }

    try:
        data = json.loads(
            STATE_FILE.read_text()
        )

        if not isinstance(data, dict):
            raise ValueError()

        if not isinstance(
            data.get("versions", {}),
            dict,
        ):
            raise ValueError()

        return data

    except Exception:
        sys.exit(
            f"Invalid state file: {STATE_FILE}"
        )


def save_state(state):
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    os.chmod(
        STATE_DIR,
        0o700,
    )

    atomic_write(
        STATE_FILE,
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        ) + "\n",
        0o600,
    )


# ------------------------------------------------------------
# Debian
# ------------------------------------------------------------

def debian_candidates():
    """
    Return:
        {(package, arch): candidate_version}
    """

    output = run(
        [
            "apt",
            "list",
            "--upgradable",
        ],
        capture=True,
    )

    packages = {}

    for line in output.splitlines():

        if line.startswith("Listing"):
            continue

        match = re.match(
            r"^([^/]+)/\S+\s+(\S+)\s+(\S+)\s+\[",
            line,
        )

        if not match:
            continue

        name = match.group(1)
        version = match.group(2)
        arch = match.group(3)

        packages[(name, arch)] = version

    return packages


def debian_security_packages(candidates):
    """
    Determine whether the exact APT candidate comes from a
    Debian security repository.

    If we cannot determine the origin reliably, the package
    goes into 'uncertain' and is NOT installed by the delayed
    updater.

    This is deliberately fail-closed.
    """

    security = set()
    uncertain = set()

    for (name, arch), version in candidates.items():

        output = run(
            [
                "apt-cache",
                "policy",
                f"{name}:{arch}",
            ],
            capture=True,
        )

        active_version = False
        saw_source = False
        security_source = False

        for line in output.splitlines():

            stripped = line.strip()

            version_match = re.match(
                r"^(?:\*\*\*\s+)?(\S+)\s+\d+",
                stripped,
            )

            if version_match:
                active_version = (
                    version_match.group(1)
                    == version
                )

                continue

            if not active_version:
                continue

            if (
                "http://" in line
                or "https://" in line
                or "file:" in line
            ):
                saw_source = True

                if (
                    "Debian-Security" in line
                    or re.search(
                        r"\b\S+-security\b",
                        line,
                    )
                ):
                    security_source = True

        key = (name, arch)

        if security_source:
            security.add(key)

        elif not saw_source:
            uncertain.add(key)

    return security, uncertain


def configure_debian_security_updates():
    print()
    print("Configuring Debian security updates...")

    run([
        "apt-get",
        "update",
    ])

    run([
        "apt-get",
        "install",
        "-y",
        "unattended-upgrades",
    ])

    # Override the Debian defaults so unattended-upgrades only
    # installs packages from the security archive.

    config = r'''// Managed by delayed-upgrades

#clear Unattended-Upgrade::Origins-Pattern;

Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};

Unattended-Upgrade::Automatic-Reboot "false";
'''

    atomic_write(
        "/etc/apt/apt.conf.d/"
        "52delayed-upgrades-security-only",
        config,
    )

    periodic = r'''// Managed by delayed-upgrades

APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
'''

    atomic_write(
        "/etc/apt/apt.conf.d/20auto-upgrades",
        periodic,
    )

    run([
        "systemctl",
        "enable",
        "--now",
        "apt-daily.timer",
    ])

    run([
        "systemctl",
        "enable",
        "--now",
        "apt-daily-upgrade.timer",
    ])


# ------------------------------------------------------------
# Fedora
# ------------------------------------------------------------

def fedora_candidates():
    """
    Get Fedora updates through DNF5 JSON output.
    """

    output = run(
        [
            "dnf5",
            "list",
            "--upgrades",
            "--json",
        ],
        capture=True,
        allowed=(0, 100),
    )

    data = json.loads(
        output or "{}"
    )

    packages = {}

    # DNF5 JSON layout can contain one or more lists.
    for value in data.values():

        if not isinstance(value, list):
            continue

        for package in value:

            if not isinstance(package, dict):
                continue

            name = package.get("name")
            arch = package.get("arch")
            evr = package.get("evr")

            if name and arch and evr:
                packages[(name, arch)] = evr

    return packages


def fedora_security_packages(candidates):
    """
    Get updates covered by currently available security
    advisories.

    Packages in this set are excluded from the delayed path.
    """

    output = run(
        [
            "dnf5",
            "advisory",
            "list",
            "--security",
            "--available",
            "--json",
        ],
        capture=True,
    )

    data = json.loads(
        output or "[]"
    )

    security = set()

    for advisory in data:

        if not isinstance(advisory, dict):
            continue

        nevra = advisory.get(
            "nevra",
            "",
        )

        for key, version in candidates.items():

            name, arch = key

            if (
                nevra.startswith(
                    name + "-"
                )
                and nevra.endswith(
                    f"-{version}.{arch}"
                )
            ):
                security.add(key)

    return security


def configure_fedora_security_updates():
    print()
    print("Configuring Fedora security updates...")

    if not shutil.which("dnf5"):
        sys.exit(
            "DNF5 was not found. "
            "This script requires a Fedora release using DNF5."
        )

    run([
        "dnf5",
        "-y",
        "install",
        "dnf5-plugin-automatic",
    ])

    automatic_config = r'''# Managed by delayed-upgrades

[commands]
upgrade_type = security
download_updates = true
apply_updates = true
random_sleep = 0
'''

    atomic_write(
        "/etc/dnf/automatic.conf",
        automatic_config,
    )

    run([
        "systemctl",
        "enable",
        "--now",
        "dnf5-automatic.timer",
    ])


# ------------------------------------------------------------
# Package queries
# ------------------------------------------------------------

def get_current_updates():
    distro = get_distribution()

    if distro == "debian":

        run([
            "apt-get",
            "update",
        ])

        candidates = debian_candidates()

        security, uncertain = (
            debian_security_packages(
                candidates
            )
        )

    else:

        run([
            "dnf5",
            "makecache",
            "--refresh",
        ])

        candidates = fedora_candidates()

        security = (
            fedora_security_packages(
                candidates
            )
        )

        uncertain = set()

    return (
        candidates,
        security,
        uncertain,
    )


# ------------------------------------------------------------
# Quarantine
# ------------------------------------------------------------

def scan():
    candidates, security, uncertain = (
        get_current_updates()
    )

    now = int(time.time())

    old_state = load_state()

    old_versions = old_state.get(
        "versions",
        {},
    )

    new_state = {
        "versions": {},
        "last_scan": now,
    }

    eligible = {}

    print()
    print("Current updates:")
    print()

    for key, version in sorted(
        candidates.items()
    ):

        name, arch = key

        identifier = (
            f"{name}|{arch}|{version}"
        )

        previous = old_versions.get(
            identifier
        )

        if previous:
            first_seen = int(
                previous["first_seen"]
            )
        else:
            first_seen = now

        age = now - first_seen

        new_state["versions"][identifier] = {
            "name": name,
            "arch": arch,
            "version": version,
            "first_seen": first_seen,
        }

        if key in security:

            status = "SECURITY"

        elif key in uncertain:

            # Never install something automatically when its
            # repository classification is uncertain.
            status = "SKIP-UNCERTAIN"

        elif age >= DELAY_HOURS * 3600:

            status = "ELIGIBLE"

            eligible[key] = version

        else:

            remaining = max(
                0,
                DELAY_HOURS * 3600 - age,
            )

            status = (
                f"WAIT {remaining // 3600}h"
            )

        print(
            f"{status:16} "
            f"{name}.{arch} "
            f"{version}"
        )

    # Versions no longer offered disappear here.
    #
    # Therefore:
    #
    # foo 1.0 appears -> timestamp recorded
    # foo 1.1 replaces it -> 1.0 disappears
    #                         1.1 gets new timestamp
    #
    # This resets the quarantine automatically.

    save_state(
        new_state
    )

    return eligible


# ------------------------------------------------------------
# Installation
# ------------------------------------------------------------

def install_eligible(eligible):
    if not eligible:

        print()
        print(
            "No quarantined non-security "
            "updates are ready."
        )

        return

    print()
    print(
        "Revalidating repositories before installation..."
    )

    # Important:
    #
    # Refresh metadata again immediately before installing.
    # A package must still:
    #
    # 1. have exactly the same version
    # 2. not be classified as security
    # 3. not have uncertain origin
    #
    # This narrows the race between scan and installation.

    candidates, security, uncertain = (
        get_current_updates()
    )

    safe = {}

    for key, version in eligible.items():

        name, arch = key

        if candidates.get(key) != version:

            print(
                "SKIP-CHANGED     "
                f"{name}.{arch} {version}"
            )

            continue

        if key in security:

            print(
                "SKIP-SECURITY    "
                f"{name}.{arch} {version}"
            )

            continue

        if key in uncertain:

            print(
                "SKIP-UNCERTAIN   "
                f"{name}.{arch} {version}"
            )

            continue

        safe[key] = version

    if not safe:

        print()
        print(
            "Nothing remains eligible "
            "after revalidation."
        )

        return

    print()
    print("Installing:")

    for (name, arch), version in safe.items():
        print(
            f"  {name}.{arch} {version}"
        )

    if get_distribution() == "debian":

        packages = [
            f"{name}:{arch}={version}"
            for (name, arch), version
            in safe.items()
        ]

        run([
            "apt-get",
            "-y",
            "--only-upgrade",
            "install",
            *packages,
        ])

    else:

        packages = [
            f"{name}.{arch}-{version}"
            for (name, arch), version
            in safe.items()
        ]

        run([
            "dnf5",
            "-y",
            "upgrade",
            *packages,
        ])


# ------------------------------------------------------------
# systemd
# ------------------------------------------------------------

def install_systemd_units():

    service = f"""[Unit]
Description=Install non-security updates after {DELAY_HOURS} hour quarantine
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={DEST} run
Nice=10
"""

    timer = """[Unit]
Description=Check delayed package updates daily

[Timer]
OnCalendar=daily
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
"""

    atomic_write(
        SERVICE_FILE,
        service,
    )

    atomic_write(
        TIMER_FILE,
        timer,
    )

    run([
        "systemctl",
        "daemon-reload",
    ])

    run([
        "systemctl",
        "enable",
        "--now",
        "delayed-upgrades.timer",
    ])


# ------------------------------------------------------------
# Setup
# ------------------------------------------------------------

def setup():
    require_root()

    distro = get_distribution()

    print()
    print("==============================")
    print(" Delayed upgrades installation")
    print("==============================")
    print()
    print(
        f"Distribution: {distro}"
    )
    print(
        f"Normal update delay: "
        f"{DELAY_HOURS} hours"
    )
    print()

    if distro == "debian":

        configure_debian_security_updates()

    elif distro == "fedora":

        configure_fedora_security_updates()

    # Install this same script as the worker.

    source = Path(
        __file__
    ).resolve()

    if source != DEST:

        atomic_write(
            DEST,
            source.read_text(),
            0o755,
        )

    else:

        os.chmod(
            DEST,
            0o755,
        )

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    os.chmod(
        STATE_DIR,
        0o700,
    )

    install_systemd_units()

    print()
    print("==============================")
    print(" Initial quarantine scan")
    print("==============================")

    # Initial scan only records timestamps.
    # It cannot install newly observed normal updates.

    scan()

    print()
    print("==============================")
    print(" Installation complete")
    print("==============================")
    print()

    print(
        "Security updates:"
        " automatic ASAP"
    )

    print(
        f"Other updates:"
        f" {DELAY_HOURS} hour quarantine"
    )

    print()
    print(
        "Installed command:"
    )

    print(
        f"  {DEST}"
    )

    print()
    print("Useful commands:")

    print(
        "  sudo delayed-upgrades status"
    )

    print(
        "  sudo delayed-upgrades check"
    )

    print(
        "  sudo delayed-upgrades run"
    )

    print(
        "  systemctl list-timers "
        "delayed-upgrades.timer"
    )

    print(
        "  journalctl "
        "-u delayed-upgrades.service"
    )

    print()


# ------------------------------------------------------------
# Status
# ------------------------------------------------------------

def status():
    state = load_state()

    versions = state.get(
        "versions",
        {},
    )

    if not versions:

        print(
            "No package versions "
            "are currently quarantined."
        )

        return

    now = int(
        time.time()
    )

    print(
        f"{'PACKAGE':35} "
        f"{'VERSION':30} "
        f"AGE"
    )

    print(
        "-" * 80
    )

    records = sorted(
        versions.values(),
        key=lambda item: item["name"],
    )

    for record in records:

        age = (
            now
            - int(record["first_seen"])
        ) // 3600

        package = (
            f'{record["name"]}.'
            f'{record["arch"]}'
        )

        print(
            f"{package:35} "
            f'{record["version"]:30} '
            f"{age}h"
        )

    last_scan = state.get(
        "last_scan"
    )

    if last_scan:

        timestamp = (
            datetime
            .fromtimestamp(last_scan)
            .astimezone()
            .isoformat(
                timespec="seconds"
            )
        )

        print()
        print(
            f"Last scan: {timestamp}"
        )

    print(
        f"Quarantine: {DELAY_HOURS}h"
    )


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main():

    if len(sys.argv) >= 2:
        action = sys.argv[1]

    elif Path(sys.argv[0]).name == "delayed-upgrades":
        action = "run"

    else:
        action = "setup"

    if action == "setup":

        setup()

    elif action == "check":

        require_root()

        scan()

    elif action == "run":

        require_root()

        eligible = scan()

        install_eligible(
            eligible
        )

    elif action == "status":

        status()

    else:

        print(
            "Usage:"
        )

        print(
            "  setup-delayed-upgrades.py setup"
        )

        print(
            "  delayed-upgrades check"
        )

        print(
            "  delayed-upgrades run"
        )

        print(
            "  delayed-upgrades status"
        )

        sys.exit(2)


if __name__ == "__main__":
    main()

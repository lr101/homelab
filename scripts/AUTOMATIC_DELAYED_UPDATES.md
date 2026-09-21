Delayed Upgrades

Automatic updates for Debian and Fedora with a 4-day quarantine for non-security updates.

- 🔒 Security updates are installed automatically ASAP
- ⏳ Other updates wait 96 hours
- 🔄 A new package version restarts the 96-hour timer
- 🛡️ Unknown/uncertain updates fail closed
- ⚙️ Runs automatically via systemd

Install

chmod +x setup-delayed-updates.py
sudo ./setup-delayed-updates.py

The script installs and configures all required packages, services and timers.

Usage

# Show quarantined packages
sudo delayed-upgrades status

# Check without installing
sudo delayed-upgrades check

# Check and install eligible updates
sudo delayed-upgrades run

# View logs
journalctl -u delayed-upgrades.service

# Check next run
systemctl list-timers delayed-upgrades.timer

Non-security updates are checked daily. Security updates are handled separately by "unattended-upgrades" on Debian and "dnf5-automatic" on Fedora.

State is stored in "/var/lib/delayed-upgrades/state.json".

Uninstall

sudo systemctl disable --now delayed-upgrades.timer
sudo rm -f /etc/systemd/system/delayed-upgrades.{service,timer}
sudo rm -f /usr/local/sbin/delayed-upgrades
sudo rm -rf /var/lib/delayed-upgrades
sudo systemctl daemon-reload

This removes the delayed-update mechanism. It does not undo the security-update configuration created during installation.

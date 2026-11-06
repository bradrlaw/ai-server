#!/usr/bin/env bash
# Install the weekly backup-clone sync timer (needs a prior `backup-clone.sh clone`).
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-backup-clone.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }
cat >/etc/systemd/system/backup-clone.service <<'U'
[Unit]
Description=Sync boot disk to the WD backup clone (ADR-0038)

[Service]
Type=oneshot
ExecStart=/srv/ai/scripts/backup-clone.sh sync
TimeoutStartSec=6h
U
cat >/etc/systemd/system/backup-clone.timer <<'U'
[Unit]
Description=Weekly boot-disk backup sync

[Timer]
OnCalendar=Sun *-*-* 04:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
U
systemctl daemon-reload
systemctl enable --now backup-clone.timer
systemctl list-timers backup-clone.timer --no-pager

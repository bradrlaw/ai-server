#!/bin/bash
# Run with sudo. Disables MGLRU and makes kernel oops/lockups reboot the box (ADR-0035).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
D=/srv/ai/config
install -m 644 "$D/sysctl/90-ai-server-crash.conf" /etc/sysctl.d/90-ai-server-crash.conf
install -m 644 "$D/tmpfiles/lru-gen-off.conf" /etc/tmpfiles.d/lru-gen-off.conf
sysctl --system >/dev/null
systemd-tmpfiles --create /etc/tmpfiles.d/lru-gen-off.conf
echo "lru_gen/enabled = $(cat /sys/kernel/mm/lru_gen/enabled)  (want 0x0000)"
sysctl kernel.panic_on_oops kernel.softlockup_panic kernel.hung_task_panic kernel.panic

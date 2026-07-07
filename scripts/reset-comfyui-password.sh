#!/usr/bin/env bash
# reset-comfyui-password.sh — reset the LOCKED ComfyUI instance's login password.
#
# ComfyUI-Login stores a bcrypt hash at /srv/ai/comfyui/login/PASSWORD and also
# caches it in memory, so resetting requires deleting the file AND restarting the
# service. After running this, open http://<host>:8189/login and choose a new
# password on the (now first-time) login screen.
#
# Run with sudo:  sudo /srv/ai/scripts/reset-comfyui-password.sh
set -euo pipefail

PW=/srv/ai/comfyui/login/PASSWORD
SVC=comfyui-secure.service

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:  sudo $0" >&2
  exit 1
fi

if [[ -f "$PW" ]]; then
  rm -f "$PW"
  echo "Removed $PW"
else
  echo "No password file at $PW (already unset)."
fi

echo "Restarting $SVC to clear the cached password..."
systemctl restart "$SVC"

cat <<EOF

Done. The locked ComfyUI has no password set.
Open  http://<host>:8189/login  and choose a NEW password on first visit.
EOF

#!/usr/bin/env bash
# reset-comfyui-password.sh — reset the LOCKED ComfyUI instance's login password.
#
# ComfyUI-Login stores a bcrypt hash at /srv/ai/comfyui/login/PASSWORD and also
# caches it in memory, so resetting requires deleting the file AND restarting the
# service. After running this, open http://<host>:8189/login and choose a new
# password on the (now first-time) login screen.
#
# The secure MCP bridge (comfyui-mcp-secure) uses the first line of that file as
# its token and re-reads it whenever it changes, so it picks up the new password
# by itself. No bridge restart is needed (audit B6).
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
# The unit skips start (exit 0) when the encrypted storage is locked.
if ! systemctl is-active --quiet "$SVC"; then
  echo "NOTE: $SVC is not running (encrypted storage locked?)." >&2
  echo "      Run 'sudo /srv/ai/scripts/storage-crypt.sh unlock' to start it, then set the password." >&2
fi

cat <<EOF

Done. The locked ComfyUI has no password set.
Open  http://<host>:8189/login  and choose a NEW password on first visit.
The secure MCP bridge (Open WebUI's comfyui-secure tools, aichat) picks up the
new password on its next call; until it is set, those calls fail with 401.
EOF

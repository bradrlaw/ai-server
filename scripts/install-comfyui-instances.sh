#!/usr/bin/env bash
# install-comfyui-instances.sh — switch ComfyUI from a single instance to two:
#   * comfyui-open.service   — no auth,  V100 #1 (idx1), :8188  (MCP + Open WebUI + open canvas)
#   * comfyui-secure.service — password, V100 #2 (idx2), :8189  (private canvas, ComfyUI-Login)
#
# Shared: models/, custom_nodes/, --user-directory (workflows + settings).
# Separate: output/input/temp dirs (asset isolation between the two instances).
#
# Run with sudo:  sudo /srv/ai/scripts/install-comfyui-instances.sh
set -euo pipefail

SRC=/srv/ai/scripts
DEST=/etc/systemd/system
COMFY=/srv/ai/comfyui

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root:  sudo $0" >&2
  exit 1
fi

echo "==> Sanity checks"
for f in comfyui-open.service comfyui-secure.service comfyui-secure-extra-paths.yaml; do
  [[ -f "$SRC/$f" ]] || { echo "missing $SRC/$f" >&2; exit 1; }
done
[[ -d "$COMFY/custom_nodes_secure/ComfyUI-Login" ]] || {
  echo "ComfyUI-Login not found in $COMFY/custom_nodes_secure — run the (non-sudo) setup first." >&2; exit 1; }
for d in output-open input-open temp-open output-secure input-secure temp-secure user; do
  [[ -d "$COMFY/$d" ]] || { echo "missing dir $COMFY/$d" >&2; exit 1; }
done

echo "==> Stopping & disabling the old single-instance comfyui.service (if present)"
systemctl stop    comfyui.service 2>/dev/null || true
systemctl disable comfyui.service 2>/dev/null || true

echo "==> Installing new unit files"
install -m 0644 "$SRC/comfyui-open.service"   "$DEST/comfyui-open.service"
install -m 0644 "$SRC/comfyui-secure.service" "$DEST/comfyui-secure.service"

echo "==> Reloading systemd, enabling and (re)starting both instances"
systemctl daemon-reload
# enable for boot, then RESTART (not `enable --now`): `--now` only *starts* an
# inactive unit, so on an upgrade where the services are already running it would
# leave the old code + old command line live. restart applies the new unit file
# and reloads the process (picking up the current free_gpu hook).
systemctl enable  comfyui-open.service comfyui-secure.service
systemctl restart comfyui-open.service comfyui-secure.service

echo
echo "==> Status"
systemctl --no-pager --lines=0 status comfyui-open.service comfyui-secure.service || true

cat <<EOF

Done. Two ComfyUI instances are now running:

  OPEN   (no auth) : http://<host>:8188   V100 #1 (idx1)   -> MCP / Open WebUI / open canvas
  LOCKED (password): http://<host>:8189   V100 #2 (idx2)   -> private canvas

NEXT STEP — set the shared password:
  Open  http://<host>:8189/login  in a browser and choose a password on first visit.
  (It is stored bcrypt-hashed at $COMFY/login/PASSWORD; delete that file to reset.)

Logs:
  journalctl -u comfyui-open.service   -f
  journalctl -u comfyui-secure.service -f
EOF

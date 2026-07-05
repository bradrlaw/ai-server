#!/usr/bin/env bash
# Install the ComfyUI systemd service (Phase 6, generative media — burst V100).
# ComfyUI is a headless web server (port 8188); connect from a browser over
# LAN/Tailscale. It is a BURST workload that competes with the `coding` model on
# its pinned V100, so it is installed but NOT enabled at boot by default.
#
# Prereqs: venv at /srv/ai/venvs/comfyui, repo at /srv/ai/comfyui, models present.
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-comfyui-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
PY=/srv/ai/venvs/comfyui/bin/python
APP=/srv/ai/comfyui/main.py
UNIT=/etc/systemd/system/comfyui.service

# sanity checks
[[ -x "$PY" ]]  || { echo "ComfyUI venv python missing at $PY"; exit 1; }
[[ -f "$APP" ]] || { echo "ComfyUI main.py missing at $APP"; exit 1; }

mkdir -p /srv/ai/comfyui/output
chown brad:brad /srv/ai/comfyui/output

# Install the free_gpu hook (on Queue, unloads only the model(s) on ComfyUI's
# idx1 card via llama-swap per-model unload; keeps chat+fast loaded — no manual
# unload for the family).
mkdir -p /srv/ai/comfyui/custom_nodes
install -m644 "$SRC/comfyui-free-gpu-node.py" /srv/ai/comfyui/custom_nodes/free_gpu.py

# Install ComfyUI-Manager (server-side installer for missing models/nodes, so the
# family can add models from the web UI — downloads land server-side in
# /srv/ai/comfyui/models/). Idempotent: clone if absent, then install its deps.
MGR=/srv/ai/comfyui/custom_nodes/comfyui-manager
if [[ ! -d "$MGR/.git" ]]; then
  sudo -u brad git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager.git "$MGR"
fi
sudo -u brad /srv/ai/venvs/comfyui/bin/pip install -q -r "$MGR/requirements.txt"

chown -R brad:brad /srv/ai/comfyui/custom_nodes

install -m644 "$SRC/comfyui.service" "$UNIT"
systemctl daemon-reload
# Start now for testing, but do NOT enable at boot (burst workload).
systemctl start comfyui.service

sleep 4
systemctl --no-pager --full status comfyui.service | head -12
echo
echo "ComfyUI starting on http://<host>:8188  (pinned to CUDA_VISIBLE_DEVICES in the unit)."
echo "Logs:   journalctl -u comfyui -f"
echo "Stop:   sudo systemctl stop comfyui        (frees the V100 for LLMs)"
echo "Boot:   sudo systemctl enable comfyui      (optional — only if you want it always-on)"

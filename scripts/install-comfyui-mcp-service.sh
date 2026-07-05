#!/usr/bin/env bash
# Install the ComfyUI MCP server systemd service (image-gen tools for mcpo/agents).
#
# This is a CPU-only bridge that exposes the (vendored) joenorton/comfyui-mcp-server
# over streamable-http on 0.0.0.0:9000, talking to native ComfyUI at 127.0.0.1:8188.
# mcpo proxies it to Open WebUI + agents (docker/mcpo/config.json).
#
# Prereqs:
#   - upstream clone at /srv/ai/src/comfyui-mcp-server (git clone, kept pristine)
#   - venv at /srv/ai/venvs/comfyui-mcp with requirements.txt + mcp[cli] installed
#   - style workflows in /srv/ai/config/comfyui-mcp/workflows/
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-comfyui-mcp-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
PY=/srv/ai/venvs/comfyui-mcp/bin/python
LAUNCH=/srv/ai/scripts/comfyui-mcp-launch.py
CLONE=/srv/ai/src/comfyui-mcp-server
WFDIR=/srv/ai/config/comfyui-mcp/workflows
UNIT=/etc/systemd/system/comfyui-mcp.service

# sanity checks
[[ -x "$PY" ]]      || { echo "comfyui-mcp venv python missing at $PY"; exit 1; }
[[ -f "$LAUNCH" ]]  || { echo "launcher missing at $LAUNCH"; exit 1; }
[[ -f "$CLONE/server.py" ]] || { echo "upstream clone missing at $CLONE"; exit 1; }
[[ -d "$WFDIR" ]]   || { echo "workflow dir missing at $WFDIR"; exit 1; }

install -m644 "$SRC/comfyui-mcp.service" "$UNIT"
systemctl daemon-reload
# Bridge is cheap (no GPU) and useful whenever ComfyUI is up — enable at boot.
systemctl enable comfyui-mcp.service
systemctl restart comfyui-mcp.service

sleep 4
systemctl --no-pager --full status comfyui-mcp.service | head -12

# mcpo does not retry backends that were down at its startup, so bounce it now that
# the :9000 service is up to (re)establish the 'comfyui' connection. Run as the repo
# owner so it uses brad's docker/compose context.
if command -v docker >/dev/null 2>&1; then
  sudo -u brad sh -lc 'cd /srv/ai/docker && docker compose restart mcpo' || \
    echo "NOTE: could not restart mcpo automatically — run: cd /srv/ai/docker && docker compose restart mcpo"
fi

echo
echo "ComfyUI MCP server on http://<host>:9000/mcp (streamable-http, NO auth — LAN/Tailscale only)."
echo "Logs:    journalctl -u comfyui-mcp -f"
echo "Restart: sudo systemctl restart comfyui-mcp"
echo "mcpo:    exposed to Open WebUI/agents via docker/mcpo/config.json (comfyui entry)."

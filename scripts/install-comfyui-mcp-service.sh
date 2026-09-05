#!/usr/bin/env bash
# Install the ComfyUI MCP server systemd service(s) (image-gen tools for mcpo/agents).
#
# Two CPU-only bridges run the (vendored) joenorton/comfyui-mcp-server over
# streamable-http, one per ComfyUI instance:
#   comfyui-mcp.service         -> 0.0.0.0:9000 -> native ComfyUI :8188 (OPEN)
#   comfyui-mcp-secure.service  -> 0.0.0.0:9001 -> native ComfyUI :8189 (SECURE, login-gated)
# mcpo proxies both to Open WebUI + agents (docker/mcpo/config.json: comfyui / comfyui-secure).
#
# Prereqs:
#   - upstream clone at /srv/ai/src/comfyui-mcp-server (git clone at e0101b2, then
#     `git am /srv/ai/scripts/patches/comfyui-mcp-server-local.patch` for the local fixes)
#   - venv at /srv/ai/venvs/comfyui-mcp with requirements.txt + mcp[cli] installed
#   - style workflows in /srv/ai/config/comfyui-mcp/workflows/
#   - secure bridge only: /srv/ai/comfyui/login/PASSWORD exists (ComfyUI-Login token)
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-comfyui-mcp-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
PY=/srv/ai/venvs/comfyui-mcp/bin/python
LAUNCH=/srv/ai/scripts/comfyui-mcp-launch.py
CLONE=/srv/ai/src/comfyui-mcp-server
WFDIR=/srv/ai/config/comfyui-mcp/workflows

# sanity checks
[[ -x "$PY" ]]      || { echo "comfyui-mcp venv python missing at $PY"; exit 1; }
[[ -f "$LAUNCH" ]]  || { echo "launcher missing at $LAUNCH"; exit 1; }
[[ -f "$CLONE/server.py" ]] || { echo "upstream clone missing at $CLONE"; exit 1; }
[[ -d "$WFDIR" ]]   || { echo "workflow dir missing at $WFDIR"; exit 1; }

# Install both units: open (:9000) and secure (:9001).
for svc in comfyui-mcp comfyui-mcp-secure; do
  install -m644 "$SRC/$svc.service" "/etc/systemd/system/$svc.service"
done
systemctl daemon-reload
# Bridges are cheap (no GPU) and useful whenever ComfyUI is up — enable at boot.
for svc in comfyui-mcp comfyui-mcp-secure; do
  systemctl enable "$svc.service"
  systemctl restart "$svc.service"
done

sleep 4
for svc in comfyui-mcp comfyui-mcp-secure; do
  systemctl --no-pager --full status "$svc.service" | head -12
  echo
done

# mcpo does not retry backends that were down at its startup, so bounce it now that
# the :9000/:9001 services are up to (re)establish the 'comfyui'/'comfyui-secure'
# connections. Run as the repo owner so it uses brad's docker/compose context.
if command -v docker >/dev/null 2>&1; then
  sudo -u brad sh -lc 'cd /srv/ai/docker && docker compose restart mcpo' || \
    echo "NOTE: could not restart mcpo automatically — run: cd /srv/ai/docker && docker compose restart mcpo"
fi

echo
echo "ComfyUI MCP bridges (streamable-http, NO auth on the bridge — LAN/Tailscale only):"
echo "  open   -> http://<host>:9000/mcp  (ComfyUI :8188)"
echo "  secure -> http://<host>:9001/mcp  (ComfyUI :8189, login-gated; bridge injects the token)"
echo "Logs:    journalctl -u comfyui-mcp -f   |   journalctl -u comfyui-mcp-secure -f"
echo "Restart: sudo systemctl restart comfyui-mcp comfyui-mcp-secure"
echo "mcpo:    exposed to Open WebUI/agents via docker/mcpo/config.json (comfyui / comfyui-secure entries)."

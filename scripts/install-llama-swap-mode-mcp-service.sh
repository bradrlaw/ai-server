#!/usr/bin/env bash
# Install the llama-swap mode-switch MCP server as a native systemd HTTP service.
#
# Exposes the config-mode switcher (scripts/llama-swap-mode.py, via
# docker/mcpo/llama_swap_mode_mcp.py) over streamable-http on 0.0.0.0:9120 so:
#   * Open WebUI reaches it through mcpo (docker/mcpo/config.json:
#     "llama-swap-mode" -> streamable-http host.docker.internal:9120/mcp), and
#   * a dependency-free Copilot BYOK client can register it with:
#         copilot mcp add --transport http llama-swap-mode http://<host>:9120/mcp
#
# It runs NATIVELY on the host (not in the mcpo container) because it edits
# config/llama-swap.yaml and talks to llama-swap on 127.0.0.1:9090. Reuses the
# comfyui-mcp venv (already has `mcp`); the switcher itself is invoked with the
# system python3 (has PyYAML).
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-llama-swap-mode-mcp-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

PY=/srv/ai/venvs/comfyui-mcp/bin/python
APP=/srv/ai/docker/mcpo/llama_swap_mode_mcp.py
SWITCH=/srv/ai/scripts/llama-swap-mode.py
UNIT_SRC=/srv/ai/scripts/llama-swap-mode-mcp.service
UNIT=/etc/systemd/system/llama-swap-mode-mcp.service

# sanity checks
[[ -x "$PY" ]]       || { echo "venv python missing at $PY (expected the comfyui-mcp venv)"; exit 1; }
[[ -f "$APP" ]]      || { echo "MCP server missing at $APP"; exit 1; }
[[ -f "$SWITCH" ]]   || { echo "switcher missing at $SWITCH"; exit 1; }
[[ -f "$UNIT_SRC" ]] || { echo "unit file missing at $UNIT_SRC"; exit 1; }

install -m644 "$UNIT_SRC" "$UNIT"
systemctl daemon-reload
systemctl enable llama-swap-mode-mcp.service
systemctl restart llama-swap-mode-mcp.service

sleep 3
systemctl --no-pager --full status llama-swap-mode-mcp.service | head -12

echo
echo "llama-swap-mode MCP on http://<host>:9120/mcp (streamable-http, NO auth — LAN/Tailscale only)."
echo "Open WebUI reaches it via mcpo (already registered in docker/mcpo/config.json)."
echo "Register on a Copilot BYOK client:"
echo "    copilot mcp add --transport http llama-swap-mode http://<host-or-tailscale>:9120/mcp"
echo "Logs:    journalctl -u llama-swap-mode-mcp -f"
echo "Restart: sudo systemctl restart llama-swap-mode-mcp"

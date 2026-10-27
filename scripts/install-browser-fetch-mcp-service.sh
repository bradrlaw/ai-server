#!/usr/bin/env bash
# Install the headless-browser fetch MCP server as a native systemd HTTP service
# (0.0.0.0:9130, endpoint /mcp). See docker/mcpo/browser_fetch_mcp.py and ADR-0034.
# Prereq (non-privileged, already done by the agent): venvs/browser with playwright,
# mcp<2 and Chromium under venvs/browser/pw.
# Then re-run the firewall so containers can reach :9130 (ADR-0028):
#   sudo /srv/ai/scripts/setup-firewall.sh
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-browser-fetch-mcp-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

PY=/srv/ai/venvs/browser/bin/python
APP=/srv/ai/docker/mcpo/browser_fetch_mcp.py
UNIT_SRC=/srv/ai/scripts/browser-fetch-mcp.service
UNIT=/etc/systemd/system/browser-fetch-mcp.service

[[ -x "$PY" ]]       || { echo "venv python missing at $PY"; exit 1; }
[[ -d /srv/ai/venvs/browser/pw ]] || { echo "Chromium missing: PLAYWRIGHT_BROWSERS_PATH=/srv/ai/venvs/browser/pw $PY -m playwright install chromium"; exit 1; }
[[ -f "$APP" ]]      || { echo "MCP server missing at $APP"; exit 1; }
[[ -f "$UNIT_SRC" ]] || { echo "unit file missing at $UNIT_SRC"; exit 1; }

install -m644 "$UNIT_SRC" "$UNIT"
systemctl daemon-reload
systemctl enable browser-fetch-mcp.service
systemctl restart browser-fetch-mcp.service

sleep 4
systemctl --no-pager --full status browser-fetch-mcp.service | head -12

echo
echo "browser-fetch MCP on http://<host>:9130/mcp (streamable-http, NO auth — LAN/Tailscale only)."
echo "Next: sudo /srv/ai/scripts/setup-firewall.sh (adds 9130), then:"
echo "  cd /srv/ai/docker && docker compose restart mcpo"
echo "Logs: journalctl -u browser-fetch-mcp -f"

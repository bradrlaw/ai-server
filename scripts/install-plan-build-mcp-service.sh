#!/usr/bin/env bash
# Install the plan-build MCP server as a native systemd HTTP service.
#
# Exposes the planner->coder pipeline (docker/mcpo/plan_build_mcp.py) over
# streamable-http on 0.0.0.0:9100 so a dependency-free Copilot BYOK client (e.g. on
# a Mac) can register it with:
#
#     copilot mcp add --transport http plan-build http://<host-or-tailscale>:9100/mcp
#
# This is SEPARATE from the stdio path mcpo uses for Open WebUI (docker/mcpo/
# config.json) — that keeps working unchanged. Both run the same script; the
# transport is chosen by PLAN_BUILD_TRANSPORT (env-gated, default stdio).
#
# Reuses the existing comfyui-mcp venv (it already has mcp[cli] + httpx); no new venv.
# CPU-only bridge (no GPU): the heavy lifting happens on the V100s only when a tool
# is actually invoked, via LiteLLM.
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-plan-build-mcp-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

PY=/srv/ai/venvs/comfyui-mcp/bin/python
APP=/srv/ai/docker/mcpo/plan_build_mcp.py
ENVFILE=/srv/ai/docker/.env
UNIT_SRC=/srv/ai/scripts/plan-build-mcp.service
UNIT=/etc/systemd/system/plan-build-mcp.service

# sanity checks
[[ -x "$PY" ]]       || { echo "venv python missing at $PY (expected the comfyui-mcp venv)"; exit 1; }
[[ -f "$APP" ]]      || { echo "plan_build_mcp.py missing at $APP"; exit 1; }
[[ -f "$ENVFILE" ]]  || { echo "env file missing at $ENVFILE (needs LITELLM_MASTER_KEY)"; exit 1; }
[[ -f "$UNIT_SRC" ]] || { echo "unit file missing at $UNIT_SRC"; exit 1; }

install -m644 "$UNIT_SRC" "$UNIT"
systemctl daemon-reload
systemctl enable plan-build-mcp.service
systemctl restart plan-build-mcp.service

sleep 3
systemctl --no-pager --full status plan-build-mcp.service | head -12

echo
echo "plan-build MCP on http://<host>:9100/mcp (streamable-http, NO auth — LAN/Tailscale only)."
echo "Register on a Copilot BYOK client:"
echo "    copilot mcp add --transport http plan-build http://<host-or-tailscale>:9100/mcp"
echo "Logs:    journalctl -u plan-build-mcp -f"
echo "Restart: sudo systemctl restart plan-build-mcp"

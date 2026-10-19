#!/usr/bin/env bash
# Install the aichat MCP bridge as a systemd service (boot-persistent).
#
# The bridge (node mcp/bridge/index.js) reads tools/llm-functions/mcp.json, connects
# to each MCP server listed there (currently comfyui-secure on :9001) and re-exposes
# their tools over http://127.0.0.1:8808 for the llm-functions bin shims that aichat
# calls. It has no auth, so it is loopback-only twice over (ADR-0026): our bridge
# patch binds MCP_BRIDGE_HOST (default 127.0.0.1) and the unit's IPAddressAllow/Deny
# kernel filter admits only localhost. The tool DECLARATIONS already live in
# tools/llm-functions/functions.json (generated once by `argc mcp start`); this
# service only needs to keep the bridge process alive, so it does NOT regenerate
# functions.json.
#
# Prereqs (set up by the initial `argc mcp start` run):
#   - /srv/ai/tools/llm-functions cloned, mcp/bridge deps installed (npm install)
#   - /srv/ai/tools/llm-functions/mcp.json present (lists the comfyui-secure server)
#   - /srv/ai/tools/llm-functions/functions.json already contains the merged mcp tools
#   - aichat functions_dir symlinked to /srv/ai/tools/llm-functions
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-aichat-mcp-bridge.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
LF=/srv/ai/tools/llm-functions
NODE=/usr/bin/node

# sanity checks
[[ -x "$NODE" ]]               || { echo "node missing at $NODE"; exit 1; }
[[ -f "$LF/mcp/bridge/index.js" ]] || { echo "bridge missing at $LF/mcp/bridge/index.js"; exit 1; }
[[ -f "$LF/mcp.json" ]]        || { echo "mcp.json missing at $LF/mcp.json"; exit 1; }
[[ -f "$LF/functions.json" ]]  || { echo "functions.json missing — run 'argc mcp start' in $LF first"; exit 1; }

# Stop any interactive (nohup) bridge holding :8808 so the service can bind it.
# NB: we kill the bridge by the PID it reports, NOT via `argc mcp stop`, because
# that would also unmerge the comfyui tools from functions.json.
PID="$(curl -s http://127.0.0.1:8808/pid 2>/dev/null || true)"
if [[ -n "${PID//[^0-9]/}" ]]; then
  echo "Stopping interactive bridge (pid $PID) holding :8808..."
  kill "$PID" 2>/dev/null || true
  sleep 1
fi

install -m644 "$SRC/aichat-mcp-bridge.service" /etc/systemd/system/aichat-mcp-bridge.service
systemctl daemon-reload
systemctl enable aichat-mcp-bridge.service
systemctl restart aichat-mcp-bridge.service

sleep 3
systemctl --no-pager --full status aichat-mcp-bridge.service | head -12
echo
echo "Bridge health:"; curl -s http://127.0.0.1:8808/health || echo "  (not responding yet)"
echo
echo "Tools exposed:"; curl -s http://127.0.0.1:8808/tools | jq -r '.[].name' 2>/dev/null | sed 's/^/  /'
echo
echo "Done. aichat now has the comfyui-secure tools (default model: aiserver:chat-uncensored-q6)."
echo "If you edit $LF/mcp.json, re-run 'argc mcp start' in $LF to rebuild functions.json, then"
echo "'sudo systemctl restart aichat-mcp-bridge'."

echo
echo "Verifying the bridge lockdown (scripts/verify-mcp-lockdown.sh --section aichat)..."
if ! sudo -u brad /srv/ai/scripts/verify-mcp-lockdown.sh --section aichat; then
  echo "WARNING: lockdown verification FAILED — see the FAIL lines above."
  exit 1
fi

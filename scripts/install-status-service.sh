#!/usr/bin/env bash
# Install + start the AI-server status service (JSON + HTML dashboard on :9095).
# It aggregates llama-swap / ComfyUI / GPU state and is consumed by the Open WebUI
# "Server Status" inlet filter (and browsable at http://<host>:9095/).
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-status-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
UNIT=/etc/systemd/system/server-status.service

# sanity: script parses and nvidia-smi exists
python3 -c "import ast; ast.parse(open('$SRC/server-status-service.py').read())" \
  || { echo "server-status-service.py has a syntax error"; exit 1; }
command -v nvidia-smi >/dev/null || echo "WARNING: nvidia-smi not found; GPU section will be empty."

echo "Installing unit -> $UNIT"
cp "$SRC/server-status.service" "$UNIT"
systemctl daemon-reload
systemctl enable server-status.service
systemctl restart server-status.service
sleep 2
systemctl --no-pager --full status server-status.service | head -15
echo
echo "Test:  curl -s http://127.0.0.1:9095/status.json | python3 -m json.tool"
echo "Dashboard:  http://<host-ip>:9095/"
echo "Live logs:  journalctl -u server-status -f"

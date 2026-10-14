#!/usr/bin/env bash
# Install + start the AI-server status service (JSON + HTML dashboard on :9095).
# It aggregates llama-swap / ComfyUI / GPU state and is consumed by the Open WebUI
# "Server Status" inlet filter (and browsable at http://<host>:9095/ after signing
# in — ADR-0027). If no dashboard password is set yet, this prompts for one.
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-status-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
UNIT=/etc/systemd/system/server-status.service
SVC_USER=$(sed -n 's/^User=//p' "$SRC/server-status.service")
AUTH=$(sed -n 's/^STATUS_AUTH_FILE=//p' "$SRC/server-status.env" 2>/dev/null || true)
AUTH=${AUTH:-/srv/ai/var/status-service/auth.json}
SETPW="python3 $SRC/server-status-service.py --set-password"
[[ $AUTH == /srv/ai/var/status-service/auth.json ]] || SETPW="STATUS_AUTH_FILE=$AUTH $SETPW"

# sanity: script parses and nvidia-smi exists
python3 -c "import ast; ast.parse(open('$SRC/server-status-service.py').read())" \
  || { echo "server-status-service.py has a syntax error"; exit 1; }
command -v nvidia-smi >/dev/null || echo "WARNING: nvidia-smi not found; GPU section will be empty."

# Dashboard password (stored hashed, 0600, owned by the service user — never root).
if [[ ! -s $AUTH ]]; then
  if [[ -t 0 ]]; then
    echo "No dashboard password set yet — choose one (you'll type it to sign in):"
    sudo -u "$SVC_USER" env STATUS_AUTH_FILE="$AUTH" python3 "$SRC/server-status-service.py" --set-password
  else
    echo "WARNING: no dashboard password ($AUTH); the page stays locked until you run:"
    echo "  $SETPW"
  fi
fi

echo "Installing unit -> $UNIT"
cp "$SRC/server-status.service" "$UNIT"
systemctl daemon-reload
systemctl enable server-status.service
systemctl restart server-status.service
sleep 2
systemctl --no-pager --full status server-status.service | head -15
echo
echo "Access-control checks:"
code() { curl -s -o /dev/null -w '%{http_code}' -m 5 "$@" || true; }
lan=$(ip -4 -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' || true)
fail=0
chk() { local want=$1 got=$2 what=$3
  if [[ $got == "$want" ]]; then echo "  PASS  $what ($got)"; else echo "  FAIL  $what (got $got, want $want)"; fail=1; fi; }
chk 200 "$(code http://127.0.0.1:9095/status.json)" "host reads /status.json without sign-in"
chk 303 "$(code http://127.0.0.1:9095/)" "dashboard redirects to sign-in"
chk 401 "$(code -X POST 'http://127.0.0.1:9095/actions/quiet?paused=true')" "action without session refused"
[[ -n $lan ]] && chk 401 "$(code --interface "$lan" http://127.0.0.1:9095/status.json)" \
  "LAN source ($lan) needs sign-in for /status.json"
if curl -s -D - -o /dev/null -m 5 http://127.0.0.1:9095/status.json | grep -qi '^access-control-allow-origin'; then
  echo "  FAIL  CORS header still present"; fail=1
else
  echo "  PASS  no CORS header"
fi
echo
echo "Dashboard:  http://<host-ip>:9095/  (sign in with the dashboard password)"
echo "Change password:  $SETPW  (as $SVC_USER)"
echo "Live logs:  journalctl -u server-status -f"
exit $fail

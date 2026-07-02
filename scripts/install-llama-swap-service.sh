#!/usr/bin/env bash
# Install + start the llama-swap systemd service (Phase 2, Core serving).
# Prereqs: /srv/ai/bin/llama-swap present, /srv/ai/config/llama-swap.yaml valid.
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-llama-swap-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
BIN=/srv/ai/bin/llama-swap
CFG=/srv/ai/config/llama-swap.yaml
UNIT=/etc/systemd/system/llama-swap.service

# sanity checks
[[ -x "$BIN" ]] || { echo "llama-swap binary missing/executable at $BIN"; exit 1; }
[[ -f "$CFG" ]] || { echo "config missing at $CFG"; exit 1; }

# validate config parses by launching briefly on a throwaway port
echo "Validating config (transient launch)..."
CUDA_DEVICE_ORDER=PCI_BUS_ID "$BIN" -config "$CFG" -listen 127.0.0.1:9099 &
pid=$!
sleep 3
if ! curl -fsS http://127.0.0.1:9099/v1/models >/dev/null 2>&1; then
  kill "$pid" 2>/dev/null || true
  echo "Config failed to load — aborting."; exit 1
fi
kill "$pid" 2>/dev/null || true
sleep 1
echo "Config OK."

install -m644 "$SRC/llama-swap.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now llama-swap.service

sleep 2
systemctl --no-pager --full status llama-swap.service | head -12
echo
echo "Models available:"
curl -fsS http://127.0.0.1:9090/v1/models 2>/dev/null | python3 -c \
  "import sys,json; [print(' -',m['id']) for m in json.load(sys.stdin)['data']]" 2>/dev/null \
  || echo "  (could not query /v1/models yet — check 'journalctl -u llama-swap')"

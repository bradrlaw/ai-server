#!/usr/bin/env bash
# Benchmark window daemon (started by window.sh). Keeps the production llama-swap models
# unloaded, logs the GPUs at 1 Hz, and re-warms the daily trio when it exits for any reason.
# Usage: window-daemon.sh STATE_DIR EXPIRY_SECONDS
set -uo pipefail
STATE=$1
END=$(( $(date +%s) + $2 ))
export CUDA_DEVICE_ORDER=PCI_BUS_ID

nvidia-smi --query-gpu=timestamp,index,memory.used,power.draw,temperature.gpu,temperature.memory,clocks.sm,utilization.gpu \
  --format=csv,noheader,nounits -l 1 > "$STATE/gpu.csv" 2>/dev/null &
LOGGER=$!

cleanup() {
  kill "$LOGGER" 2>/dev/null
  echo "[$(date -u +%FT%TZ)] closing: re-warming daily" >> "$STATE/window.log"
  python3 /srv/ai/scripts/llama-swap-mode.py set daily >> "$STATE/window.log" 2>&1
  echo "[$(date -u +%FT%TZ)] closed" >> "$STATE/window.log"
  rm -f "$STATE/pid"
}
trap cleanup EXIT
trap 'exit 0' TERM INT

echo "[$(date -u +%FT%TZ)] window open, expires in $2 s" >> "$STATE/window.log"
while [[ $(date +%s) -lt $END && ! -e "$STATE/close" ]]; do
  for p in /unload /api/unload; do curl -s -m 10 -o /dev/null "127.0.0.1:9090$p"; done
  sleep 4
done

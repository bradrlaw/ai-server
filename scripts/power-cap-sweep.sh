#!/usr/bin/env bash
# Power-cap sweep for a V100 (GPU index 1 by default): for each power limit, run a
# prefill+decode benchmark while the fan daemon holds 100%, and record peak HBM
# (memory) temp, min SM clock, and pp/tg throughput. Goal: find the cap that keeps
# HBM under its ~85 C throttle point for longevity/noise, and see the perf cost.
#
# Needs root (nvidia-smi -pl). Leaves the gpu-fan-control service running (we WANT
# max fan during the test). Restores the default power limit on exit.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

GPU=${GPU:-1}
CAPS=(${CAPS:-250 200 175 150})
MODEL=${MODEL:-$(ls /srv/ai/models/qwen3.6-27b/*Q6_K*.gguf 2>/dev/null | head -1)}
BIN=${BIN:-/srv/ai/src/llama.cpp/build/bin/llama-bench}
NGL=${NGL:-999}                 # GPU layers to offload (lower for a tight-fit card)
PP=${PP:-2048}                  # prefill tokens
NG=${NG:-512}                   # decode tokens
COOL_TO=${COOL_TO:-55}          # cool below this (C) before each run
COOL_MAX=${COOL_MAX:-120}       # ...but wait at most this many seconds
export CUDA_DEVICE_ORDER=PCI_BUS_ID

[[ -n "$MODEL" && -f "$MODEL" ]] || { echo "model not found: $MODEL" >&2; exit 1; }
[[ -x "$BIN" ]] || { echo "llama-bench not found: $BIN" >&2; exit 1; }

DEFAULT_PL=$(nvidia-smi -i "$GPU" --query-gpu=power.default_limit --format=csv,noheader,nounits | awk '{printf "%d",$1}')
restore() {
  echo; echo "Restoring default power limit (${DEFAULT_PL}W) on GPU $GPU..."
  nvidia-smi -i "$GPU" -pl "$DEFAULT_PL" >/dev/null 2>&1 || true
}
trap restore EXIT

echo "Enabling persistence mode on GPU $GPU..."
nvidia-smi -i "$GPU" -pm 1 >/dev/null 2>&1 || echo "  (persistence mode not settable; continuing)"

q() { nvidia-smi -i "$GPU" --query-gpu="$1" --format=csv,noheader,nounits | awk '{printf "%d",$1}'; }

# Does this card expose an HBM memory-temperature sensor? (V100 yes, P100 no.)
MEM_RAW=$(nvidia-smi -i "$GPU" --query-gpu=temperature.memory --format=csv,noheader,nounits | head -1 | tr -d ' ')
if [[ "$MEM_RAW" =~ ^[0-9]+$ ]]; then HAS_MEM=1; HOTLABEL="HBM"; else HAS_MEM=0; HOTLABEL="core"; fi
THROTTLE_C=${THROTTLE_C:-85}
hot() { if (( HAS_MEM )); then q temperature.memory; else q temperature.gpu; fi; }
echo "GPU $GPU: throttle metric = ${HOTLABEL} temp (threshold ${THROTTLE_C}C), model=$(basename "$MODEL"), ngl=$NGL"

cooldown() {
  local waited=0 t
  t=$(hot)
  while (( t > COOL_TO && waited < COOL_MAX )); do
    sleep 5; waited=$((waited+5)); t=$(hot)
  done
  echo "  cooled: ${HOTLABEL}=${t}C after ${waited}s"
}

printf '\n%-6s | %-9s %-8s | %-9s %-9s | %-8s\n' \
  "capW" "peak${HOTLABEL}" "peakCore" "pp t/s" "tg t/s" "minClk"
printf -- '-------+--------------------+---------------------+---------\n'

for CAP in "${CAPS[@]}"; do
  nvidia-smi -i "$GPU" -pl "$CAP" >/dev/null 2>&1 \
    || { echo "cap ${CAP}W rejected (range?), skipping"; continue; }
  cooldown

  LOG=$(mktemp)
  CUDA_VISIBLE_DEVICES="$GPU" "$BIN" -m "$MODEL" -ngl "$NGL" -p "$PP" -n "$NG" -r 3 >"$LOG" 2>&1 &
  LPID=$!

  peak_h=0; peak_c=0; min_clk=99999
  while [[ -d /proc/$LPID ]]; do
    h=$(hot); c=$(q temperature.gpu); k=$(q clocks.sm)
    (( h > peak_h )) && peak_h=$h
    (( c > peak_c )) && peak_c=$c
    (( k > 0 && k < min_clk )) && min_clk=$k
    sleep 3
  done
  wait "$LPID" 2>/dev/null || true

  pp=$(awk -F'|' '/[[:space:]]pp[0-9]/{v=$(NF-1); gsub(/ /,"",v); split(v,a,"±"); print a[1]}' "$LOG" | tail -1)
  tg=$(awk -F'|' '/[[:space:]]tg[0-9]/{v=$(NF-1); gsub(/ /,"",v); split(v,a,"±"); print a[1]}' "$LOG" | tail -1)
  if [[ -z "$pp$tg" ]] && grep -qiE 'out of memory|failed to|error' "$LOG"; then
    printf '%-6s | %-9s %-8s | %-9s %-9s | %-8s  (LOAD FAILED - see below)\n' \
      "$CAP" "${peak_h}C" "${peak_c}C" "n/a" "n/a" "-"
    grep -iE 'out of memory|failed|error|alloc' "$LOG" | head -2 | sed 's/^/      /'
    rm -f "$LOG"; continue
  fi
  [[ -z "$pp" ]] && pp="n/a"; [[ -z "$tg" ]] && tg="n/a"
  throttled=""; (( peak_h >= THROTTLE_C )) && throttled=" <-THROTTLE"
  printf '%-6s | %-9s %-8s | %-9s %-9s | %-8s%s\n' \
    "$CAP" "${peak_h}C" "${peak_c}C" "$pp" "$tg" "${min_clk}MHz" "$throttled"
  rm -f "$LOG"
done

echo
echo "Done. (Default power limit restored on exit.)"
echo "Pick the highest cap whose peak${HOTLABEL} stays < ${THROTTLE_C}C; note the tg t/s cost."

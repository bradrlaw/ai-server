#!/usr/bin/env bash
# Post-boot GPU health check for the headless AI server.
# Verifies all expected GPUs enumerated and power caps + fan daemon are in the
# right state WITHOUT manual intervention. Safe to run any time; needs no root.
#
# Expected GPU count + caps are read from gpu-fan-control.config.json (the daemon's
# live source of truth, also edited by the status dashboard), so this check never
# drifts from what the daemon is told to enforce.
set -u

CONFIG="$(dirname "$(readlink -f "$0")")/gpu-fan-control.config.json"
EXPECT_COUNT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("expected_gpu_count", 3))' "$CONFIG" 2>/dev/null || echo 3)
declare -A EXPECT_CAP=()
while read -r i w; do EXPECT_CAP[$i]=$w; done < <(python3 -c '
import json, sys
for i, w in json.load(open(sys.argv[1])).get("power_limits", {}).items():
    print(int(i), int(w))' "$CONFIG" 2>/dev/null)
fail=0

echo "=== GPU state check $(date) ==="

# 1) All GPUs enumerated by the driver?
mapfile -t idxs < <(nvidia-smi --query-gpu=index --format=csv,noheader,nounits 2>/dev/null)
count=${#idxs[@]}
if [ "$count" -eq "$EXPECT_COUNT" ]; then
  echo "  [OK]   $count/$EXPECT_COUNT GPUs present"
else
  echo "  [FAIL] $count/$EXPECT_COUNT GPUs present (a card may have fallen off the bus)"
  echo "         lspci sees: $(lspci -nn 2>/dev/null | grep -ic nvidia) NVIDIA device(s)"
  fail=1
fi

# 2) Power caps at target?
while IFS=',' read -r idx cap; do
  idx=$(echo "$idx" | tr -d ' '); cap=$(printf '%.0f' "$(echo "$cap" | tr -d ' ')")
  want=${EXPECT_CAP[$idx]:-}
  if [ -z "$want" ]; then continue; fi
  if [ "$cap" -eq "$want" ]; then
    echo "  [OK]   GPU$idx power cap ${cap}W"
  else
    echo "  [FAIL] GPU$idx power cap ${cap}W (expected ${want}W)"
    fail=1
  fi
done < <(nvidia-smi --query-gpu=index,power.limit --format=csv,noheader,nounits 2>/dev/null)

# 3) Persistence mode on (keeps caps sticky when idle)?
while IFS=',' read -r idx pm; do
  idx=$(echo "$idx" | tr -d ' '); pm=$(echo "$pm" | tr -d ' ')
  if [ "$pm" = "Enabled" ]; then
    echo "  [OK]   GPU$idx persistence $pm"
  else
    echo "  [WARN] GPU$idx persistence $pm (cap held by 30s reconcile; usually Enabled after a clean boot)"
  fi
done < <(nvidia-smi --query-gpu=index,persistence_mode --format=csv,noheader 2>/dev/null)

# 4) Fan daemon active + reports all GPUs?
if systemctl is-active --quiet gpu-fan-control 2>/dev/null; then
  echo "  [OK]   gpu-fan-control service active"
else
  echo "  [FAIL] gpu-fan-control service NOT active"
  fail=1
fi
if journalctl -u gpu-fan-control -b --no-pager 2>/dev/null | grep -q "all $EXPECT_COUNT GPU(s) present"; then
  echo "  [OK]   daemon saw all $EXPECT_COUNT GPUs at startup"
else
  echo "  [WARN] daemon did not log 'all $EXPECT_COUNT GPUs present' this boot; last power-cap lines:"
  journalctl -u gpu-fan-control -b --no-pager 2>/dev/null | grep -iE "power cap|WARNING|present" | tail -4 | sed 's/^/           /'
fi

echo "=== $( [ "$fail" -eq 0 ] && echo 'ALL GOOD' || echo 'PROBLEMS FOUND -- see [FAIL] above' ) ==="
exit $fail

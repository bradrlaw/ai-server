#!/bin/bash
# One-off diagnostic: sweep EVERY pwm channel (pwm1-pwm5 on hwmon1/nct6792),
# one at a time in a RANDOMIZED order, and ask what fan/sound/airflow changes.
# This replaces earlier single-channel guesses (pwm1="blower", pwm3="210mm")
# that turned out to be unreliable -- do a full, unbiased pass instead.
#
# Safety: all OTHER channels are held at a safe floor duty (~40%) throughout,
# so GPUs/case still get baseline airflow during the test. GPUs should be
# idle/unloaded before running this.
#
# Run as: sudo bash /srv/ai/scripts/sweep-fan-channels.sh
# Not a permanent tool -- delete when done: rm /srv/ai/scripts/sweep-fan-channels.sh
set -euo pipefail
HW=/sys/class/hwmon/hwmon1
FLOOR=100   # ~40% duty, safe baseline for idle GPUs
FULL=255    # 100% duty

echo "Stopping gpu-fan-control.service for the test..."
systemctl stop gpu-fan-control.service

CHANNELS=(pwm1 pwm2 pwm3 pwm4 pwm5)

echo "Setting all channels to manual mode, floor duty (~40%)..."
for ch in "${CHANNELS[@]}"; do
  echo 1 > "$HW/${ch}_enable"
  echo "$FLOOR" > "$HW/$ch"
done
echo "Settling 15s at baseline before starting..."
sleep 15

# Randomize the order channels are tested in, so there's no expectation bias
# from "we're going in order 1,2,3,4,5".
ORDER=($(printf '%s\n' "${CHANNELS[@]}" | shuf))

declare -A ANSWERS
for ch in "${ORDER[@]}"; do
  echo ""
  echo "=== Testing $ch: ramping to 100% ==="
  echo "$FULL" > "$HW/$ch"
  sleep 10
  read -r -p "What changed (if anything)? Describe sound/location/feel, or 'nothing': " resp
  ANSWERS[$ch]="$resp"
  echo "$FLOOR" > "$HW/$ch"
  echo "(back to floor, settling 8s before next channel)"
  sleep 8
done

echo ""
echo "================ RESULTS ================"
for ch in "${CHANNELS[@]}"; do
  echo "$ch: ${ANSWERS[$ch]}"
done

echo ""
echo "Restoring normal fan control (gpu-fan-control.service)..."
systemctl start gpu-fan-control.service
echo "Done. Delete this script when finished: rm /srv/ai/scripts/sweep-fan-channels.sh"

#!/usr/bin/env bash
# Definitive physical pwm -> GPU mapping test for the two V100 shroud fans.
# Idea: with GPUs idle, force ONE V100 fan to MAX and the OTHER to a low floor.
# Whichever GPU gets COLDER is physically under the maxed fan. Then reverse to
# confirm. This does NOT rely on the daemon's control logic (no circular proof).
#
# Safe: stops the daemon during the test, floors (never 0) the untested fan,
# leaves the P100 fan on its safe duty, and ALWAYS restores + restarts the
# service on exit (including Ctrl-C / error).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

HW=/sys/class/hwmon/hwmon1
PWM_A=pwm4          # V100 fan A (currently mapped to GPU1)
PWM_B=pwm5          # V100 fan B (currently mapped to GPU2)
MAX=255
FLOOR=90           # ~35% duty floor, never stall
SETTLE=45          # seconds to let temps settle per phase

for p in "$PWM_A" "$PWM_B"; do
  [[ -w "$HW/$p" ]] || { echo "Missing $HW/$p" >&2; exit 1; }
done

echo "Stopping gpu-fan-control.service for the duration of the test..."
systemctl stop gpu-fan-control.service || true

# Save current enable modes to restore later.
declare -A SAVED
for p in "$PWM_A" "$PWM_B"; do
  en="$HW/${p}_enable"
  SAVED[$p]="$(cat "$en" 2>/dev/null || echo 5)"
done

restore() {
  echo
  echo "Restoring fan control and restarting service..."
  for p in "$PWM_A" "$PWM_B"; do
    echo "${SAVED[$p]:-5}" > "$HW/${p}_enable" 2>/dev/null || true
  done
  systemctl start gpu-fan-control.service || true
  echo "Done. Service restarted."
}
trap restore EXIT

set_manual() { echo 1 > "$HW/${1}_enable"; echo "$2" > "$HW/$1"; }

# GPU idle core temps for GPU1 and GPU2 (indices 1 and 2).
gtemps() {
  CUDA_DEVICE_ORDER=PCI_BUS_ID nvidia-smi \
    --query-gpu=index,temperature.gpu --format=csv,noheader,nounits |
    awk -F', *' '$1==1{g1=$2} $1==2{g2=$2} END{print g1, g2}'
}

phase() { # $1=label  $2=pwm-to-MAX  $3=pwm-to-FLOOR
  echo
  echo "=== Phase $1: $2 -> MAX, $3 -> FLOOR (GPUs idle) ==="
  set_manual "$2" "$MAX"
  set_manual "$3" "$FLOOR"
  read s1 s2 < <(gtemps); echo "  start:  GPU1=${s1}C  GPU2=${s2}C"
  for ((t=5; t<=SETTLE; t+=5)); do sleep 5; read c1 c2 < <(gtemps)
    printf "  %2ds:   GPU1=%sC  GPU2=%sC  (fan4=%s fan5=%s rpm)\n" \
      "$t" "$c1" "$c2" "$(cat $HW/fan4_input)" "$(cat $HW/fan5_input)"
  done
  read e1 e2 < <(gtemps)
  PH1G1=$s1; PH1G2=$s2; PHE_G1=$e1; PHE_G2=$e2
}

echo "Baseline (both fans to FLOOR, 20s settle):"
set_manual "$PWM_A" "$FLOOR"; set_manual "$PWM_B" "$FLOOR"; sleep 20
read b1 b2 < <(gtemps); echo "  GPU1=${b1}C  GPU2=${b2}C"

phase "A" "$PWM_A" "$PWM_B"; a_g1=$PHE_G1; a_g2=$PHE_G2
phase "B" "$PWM_B" "$PWM_A"; b_g1=$PHE_G1; b_g2=$PHE_G2

echo
echo "================ RESULT ================"
echo "End temps  pwm4=MAX phase:  GPU1=${a_g1}C  GPU2=${a_g2}C"
echo "End temps  pwm5=MAX phase:  GPU1=${b_g1}C  GPU2=${b_g2}C"
# The GPU that is colder when pwm4 is MAX (vs when pwm5 is MAX) is under pwm4.
d1=$(( a_g1 - b_g1 ))   # GPU1: (pwm4max) - (pwm5max); negative => pwm4 cools GPU1
d2=$(( a_g2 - b_g2 ))
echo "GPU1 delta (pwm4max - pwm5max) = ${d1}C   (negative => pwm4 cools GPU1)"
echo "GPU2 delta (pwm4max - pwm5max) = ${d2}C   (negative => pwm4 cools GPU2)"
echo
if (( d1 < d2 )); then
  echo ">> pwm4 physically cools GPU1, pwm5 cools GPU2  => CURRENT config is CORRECT."
else
  echo ">> pwm4 physically cools GPU2, pwm5 cools GPU1  => config is SWAPPED; fix needed:"
  echo "     V100 zone for GPU1 should use pwm5; GPU2 zone should use pwm4."
fi
echo "======================================="

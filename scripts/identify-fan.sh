#!/usr/bin/env bash
# Step 2 of GPU fan control: map each nct67xx pwmN channel to the fan it drives,
# by pulsing each PWM low->high and watching which fan tach (fanN_input) responds.
# Classifies each by MAX rpm: V100 shroud fans ~15k, P100 fan ~6k, case/CPU lower.
#
# SAFE: never drives a fan to 0 (uses a ~23% floor so the CPU fan keeps spinning),
# and restores automatic (BIOS) fan control on exit.
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/identify-fan.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

LOW=60      # ~23% duty floor (keeps every fan spinning; safe for CPU header)
HIGH=255    # 100%
DWELL_LOW=5
DWELL_HIGH=7

HW=""
for h in /sys/class/hwmon/hwmon*; do
  [[ "$(cat "$h/name" 2>/dev/null)" == nct67* ]] && HW="$h" && break
done
[[ -n "$HW" ]] || { echo "nct67xx not found. Run setup-fan-sensors.sh first."; exit 1; }
echo "Super-I/O chip: $(cat "$HW/name")  at  $HW"

PWMS=(); for p in "$HW"/pwm[0-9]; do [[ -e "$p" ]] && PWMS+=("$p"); done
FANS=(); for f in "$HW"/fan[0-9]_input; do [[ -e "$f" ]] && FANS+=("$f"); done
echo "PWM channels: ${PWMS[*]##*/}"
echo "Fan tachs:    ${FANS[*]##*/}"

# Save current enable modes so we can restore exactly.
declare -A SAVED
for p in "${PWMS[@]}"; do SAVED["$p"]="$(cat "${p}_enable" 2>/dev/null || echo 5)"; done

restore() {
  echo; echo "Restoring previous fan-control modes..."
  for p in "${PWMS[@]}"; do
    echo "${SAVED[$p]:-5}" > "${p}_enable" 2>/dev/null \
      || echo 5 > "${p}_enable" 2>/dev/null || true
  done
  echo "Done (BIOS auto restored)."
}
trap restore EXIT INT TERM

read_all() { for f in "${FANS[@]}"; do echo "$(cat "$f" 2>/dev/null || echo 0)"; done; }
classify() {  # $1 = max rpm
  local r=$1
  if   (( r >= 10000 )); then echo "V100 shroud fan (15k-max)";
  elif (( r >= 3500  )); then echo "P100 shroud fan (6k-max)?";
  elif (( r >= 300   )); then echo "case/CPU fan";
  else                        echo "nothing / no tach"; fi
}

echo
printf '%-6s | %-10s | %-8s | %s\n' "pwm" "resp fan" "max rpm" "best guess"
printf -- '-------+------------+----------+---------------------------\n'

for p in "${PWMS[@]}"; do
  name="$(basename "$p")"
  echo "${SAVED[$p]:-5}" >/dev/null
  echo 1 > "${p}_enable"                 # manual
  echo "$LOW"  > "$p"; sleep "$DWELL_LOW";  mapfile -t LOWV < <(read_all)
  echo "$HIGH" > "$p"; sleep "$DWELL_HIGH"; mapfile -t HIGHV < <(read_all)

  # which fan changed the most low->high
  best=-1; bestd=0; bestmax=0
  for i in "${!FANS[@]}"; do
    d=$(( ${HIGHV[$i]} - ${LOWV[$i]} ))
    (( d < 0 )) && d=$(( -d ))
    if (( d > bestd )); then bestd=$d; best=$i; bestmax=${HIGHV[$i]}; fi
  done

  echo "$LOW" > "$p"                      # ease back down
  if (( best >= 0 && bestd > 150 )); then
    fanname="$(basename "${FANS[$best]}")"
    printf '%-6s | %-10s | %-8s | %s\n' "$name" "$fanname" "$bestmax" "$(classify "$bestmax")"
  else
    printf '%-6s | %-10s | %-8s | %s\n' "$name" "-" "-" "no responding fan"
  fi
done

echo
echo "Map: V100 fans (~15k) -> the two V100 zones; P100 fan (~6k) -> P100 zone."
echo "Put the matching pwmN into gpu-fan-control.config.json, then run"
echo "install-fan-service.sh. (BIOS auto is being restored now.)"

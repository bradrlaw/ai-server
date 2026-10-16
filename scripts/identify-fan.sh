#!/usr/bin/env bash
# Step 2 of GPU fan control: map each nct67xx pwmN channel to the fan it drives,
# by pulsing each PWM low->high and watching which fan tach (fanN_input) responds.
# Classifies each by MAX rpm (15k-class 40 mm shroud fan / blower / case fan).
# It can't tell WHICH GPU a fan cools: confirm that with verify-gpu-fan-mapping.sh.
#
# Run with the GPUs idle. SAFE: stops gpu-fan-control for the test (it would
# fight over its channels) and restarts it on exit; never drives a fan to 0 (a
# ~23% floor; on this board's voltage-mode headers some fans stop that low for a
# few seconds, which is fine); hands each channel back as soon as it's measured
# and restores the previous fan-control modes on exit (including Ctrl-C).
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/identify-fan.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

LOW=60      # ~23% duty floor (never 0, so a CPU fan on the PWM-mode CPUFAN1 header keeps spinning)
HIGH=255    # 100%
DWELL_LOW=5
DWELL_HIGH=7
UNIT=gpu-fan-control.service

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

declare -A SAVED
DAEMON=0
restore() {
  trap - EXIT INT TERM
  echo; echo "Restoring previous fan-control modes..."
  for p in "${PWMS[@]}"; do
    [[ -n "${SAVED[$p]:-}" ]] || continue   # not saved yet = not touched yet
    echo "${SAVED[$p]}" > "${p}_enable" 2>/dev/null \
      || echo 5 > "${p}_enable" 2>/dev/null || true
  done
  if (( DAEMON )); then
    systemctl start "$UNIT" || true
    if systemctl is-active --quiet "$UNIT"; then echo "Done. $UNIT active again."
    else echo "WARNING: $UNIT NOT active — run: sudo systemctl start $UNIT" >&2; fi
  else
    echo "Done (previous modes restored; $UNIT was not running)."
  fi
}
trap restore EXIT
trap 'exit 130' INT TERM

if systemctl is-active --quiet "$UNIT"; then
  DAEMON=1
  echo "Stopping $UNIT for the test (restarted on exit)..."
  systemctl stop "$UNIT"
fi

# Save current enable modes so we can restore exactly (after the daemon's own
# exit handler has handed its channels back to BIOS auto).
for p in "${PWMS[@]}"; do SAVED["$p"]="$(cat "${p}_enable" 2>/dev/null || echo 5)"; done

read_all() { for f in "${FANS[@]}"; do echo "$(cat "$f" 2>/dev/null || echo 0)"; done; }
classify() {  # $1 = max rpm
  local r=$1
  if   (( r >= 10000 )); then echo "15k-class 40 mm shroud fan";
  elif (( r >= 3500  )); then echo "blower / mid-speed shroud fan";
  elif (( r >= 300   )); then echo "case/CPU (or large) fan";
  else                        echo "nothing / no tach"; fi
}

echo
printf '%-6s | %-10s | %-8s | %s\n' "pwm" "resp fan" "max rpm" "best guess"
printf -- '-------+------------+----------+---------------------------\n'

for p in "${PWMS[@]}"; do
  name="$(basename "$p")"
  echo 1 > "${p}_enable"                 # manual
  echo "$LOW"  > "$p"; sleep "$DWELL_LOW";  mapfile -t LOWV < <(read_all)
  echo "$HIGH" > "$p"; sleep "$DWELL_HIGH"; mapfile -t HIGHV < <(read_all)

  # which fan sped up the most low->high (signed: the previous channel's fan
  # may still be slowing down after being handed back, so it isn't mistaken)
  best=-1; bestd=0; bestmax=0
  for i in "${!FANS[@]}"; do
    d=$(( ${HIGHV[$i]} - ${LOWV[$i]} ))
    if (( d > bestd )); then bestd=$d; best=$i; bestmax=${HIGHV[$i]}; fi
  done

  # hand it straight back (BIOS auto, or whatever it was) instead of parking it low
  echo "${SAVED[$p]:-5}" > "${p}_enable" 2>/dev/null || true
  if (( best >= 0 && bestd > 150 )); then
    fanname="$(basename "${FANS[$best]}")"
    note=""
    [[ "$fanname" == "fan${name#pwm}_input" ]] || note="  (zone needs \"tach\": \"$fanname\")"
    printf '%-6s | %-10s | %-8s | %s\n' "$name" "$fanname" "$bestmax" "$(classify "$bestmax")$note"
  else
    printf '%-6s | %-10s | %-8s | %s\n' "$name" "-" "-" "no responding fan (none, no tach wire, or SATA/molex-powered)"
  fi
done

echo
echo "Map: put each GPU's shroud-fan pwmN into its zone's \"pwm\" in"
echo "gpu-fan-control.config.json (a fan with no tach here: set \"tach\": null),"
echo "then run install-fan-service.sh and, with the GPUs idle,"
echo "verify-gpu-fan-mapping.sh to prove which GPU each fan physically cools."

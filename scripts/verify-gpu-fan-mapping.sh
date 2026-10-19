#!/usr/bin/env bash
# Definitive physical pwm -> GPU mapping test for the GPU shroud fans, checked
# against the zones in gpu-fan-control.config.json (the daemon's live mapping).
# Idea: with GPUs idle, force ONE zone's fan to MAX and the others to a low floor.
# The GPU that gets COLDER is physically under the maxed fan; repeat per zone.
# This does NOT rely on the daemon's control logic (no circular proof).
#
# Also checks each fan's tach (the zone's "tach", default fanN_input) at FLOOR vs
# MAX: a fan that doesn't speed up is dead/unplugged, has no tach wire (e.g. the
# non-tach leg of a Y-splitter), or gets no speed control. On this board only
# CPUFAN1 is PWM-mode (a 3-pin fan there runs ~100%); the other headers are
# voltage-mode, where the usual cause is a SATA/molex-powered PWM hub (fans fed
# 12 V, and pin 4 carries no PWM signal) -- use a plain Y-splitter instead.
#
# Run with the GPUs idle (no inference / ComfyUI jobs) after any fan re-wiring.
# Safe: stops the daemon during the test, floors (never 0) the untested fans,
# leaves monitor-only zones alone, aborts if a tested GPU's core passes ABORT_C
# or its HBM passes ABORT_HBM_C, and ALWAYS restores + restarts the service on
# exit (including Ctrl-C / error).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

CONFIG=${CONFIG:-$(dirname "$(readlink -f "$0")")/gpu-fan-control.config.json}
MAX=255
FLOOR=${FLOOR:-90}        # ~35% duty floor, never stall
SETTLE=${SETTLE:-45}      # seconds to let temps settle per phase
ABORT_C=${ABORT_C:-70}    # restore + bail if any tested GPU core exceeds this
ABORT_HBM_C=${ABORT_HBM_C:-80}   # ... or its HBM (V100 HBM throttles ~85 C)

# Controlled single-GPU zones: "hwmon_name pwmN gpu tach" (monitor_only zones
# skipped; tach "-" = the zone's "tach" is null).
mapfile -t ZONES < <(python3 - "$CONFIG" <<'PY'
import json, sys
for z in json.load(open(sys.argv[1])).get("zones", []):
    if not z.get("monitor_only") and z.get("pwm") and len(z.get("gpus", [])) == 1:
        tach = z["tach"] if "tach" in z else f"fan{z['pwm'][3:]}_input"
        print(z.get("hwmon_name", "nct6775"), z["pwm"], z["gpus"][0], tach or "-")
PY
)
(( ${#ZONES[@]} >= 2 )) || { echo "Need >= 2 controlled single-GPU zones in $CONFIG (found ${#ZONES[@]})." >&2; exit 1; }

find_hwmon() {  # by name, like the daemon (hwmonN numbering isn't stable)
  local d; for d in /sys/class/hwmon/hwmon*; do
    [[ "$(cat "$d/name" 2>/dev/null)" == "$1" ]] && { echo "$d"; return 0; }
  done; return 1
}
declare -a PWM FAN GPU LBL SAVED
for i in "${!ZONES[@]}"; do
  read -r hwname pwm gpu tach <<<"${ZONES[$i]}"
  hw=$(find_hwmon "$hwname") || { echo "hwmon '$hwname' not found" >&2; exit 1; }
  [[ -w "$hw/$pwm" ]] || { echo "Missing $hw/$pwm" >&2; exit 1; }
  PWM[$i]="$hw/$pwm"; GPU[$i]=$gpu; LBL[$i]=$pwm
  FAN[$i]=$([[ $tach == - ]] && echo "" || echo "$hw/$tach")   # "" -> rpm reads 0
  # (no pwmN_mode shown: on the nct6792 pwm2-5 always read 1; the header decides)
  echo "config: ${pwm} -> GPU$gpu, tach ${tach/#-/none}   [$hw]"
done
GPUS=$(printf '%s\n' "${GPU[@]}" | sort -nu | paste -sd' ')

restore() {
  trap - EXIT INT TERM
  echo; echo "Restoring fan control and restarting service..."
  for i in "${!PWM[@]}"; do
    [[ -n "${SAVED[$i]:-}" ]] || continue   # not saved yet = not touched yet
    echo "${SAVED[$i]}" > "${PWM[$i]}_enable" 2>/dev/null || true
  done
  systemctl start gpu-fan-control.service || true
  if systemctl is-active --quiet gpu-fan-control.service; then echo "Done. gpu-fan-control active."
  else echo "WARNING: gpu-fan-control NOT active — run: sudo systemctl start gpu-fan-control" >&2; fi
}
trap restore EXIT
trap 'exit 130' INT TERM

echo "Stopping gpu-fan-control.service for the duration of the test..."
systemctl stop gpu-fan-control.service   # set -e: if it won't stop, exit before touching a fan
for i in "${!PWM[@]}"; do SAVED[$i]="$(cat "${PWM[$i]}_enable" 2>/dev/null || echo 5)"; done

set_manual() { echo 1 > "$1_enable"; echo "$2" > "$1"; }
declare -A CORE HBM
read_temps() {
  local idx c m
  while IFS=, read -r idx c m; do CORE[$idx]=$c; HBM[$idx]=$m; done \
    < <(nvidia-smi --query-gpu=index,temperature.gpu,temperature.memory --format=csv,noheader,nounits | tr -d ' ')
}
rpm() { cat "$1" 2>/dev/null || echo 0; }

RESULTS=""   # lines: "phase_zone gpu end_core"  +  "rpm zone floor|max value"
phase() {  # $1 = zone index to MAX; all other zones FLOOR
  local k=$1 i t g line fans
  echo; echo "=== Phase ${LBL[$k]} -> MAX (config: GPU${GPU[$k]}), others -> FLOOR ==="
  for i in "${!PWM[@]}"; do set_manual "${PWM[$i]}" "$([[ $i == "$k" ]] && echo $MAX || echo $FLOOR)"; done
  for ((t=5; t<=SETTLE; t+=5)); do
    sleep 5; read_temps; line=""
    for g in $GPUS; do
      line+="GPU$g=${CORE[$g]}C(hbm ${HBM[$g]})  "
      if (( CORE[$g] > ABORT_C )); then echo "GPU$g core ${CORE[$g]}C > ${ABORT_C}C — GPUs not idle? aborting" >&2; exit 1; fi
      if [[ ${HBM[$g]} =~ ^[0-9]+$ ]] && (( HBM[$g] > ABORT_HBM_C )); then
        echo "GPU$g HBM ${HBM[$g]}C > ${ABORT_HBM_C}C — GPUs not idle? aborting" >&2; exit 1; fi
    done
    fans=""; for i in "${!FAN[@]}"; do fans+="${LBL[$i]}:$(rpm "${FAN[$i]}") "; done
    printf "  %2ds  %s rpm[%s]\n" "$t" "$line" "${fans% }"
  done
  for g in $GPUS; do RESULTS+="T $k $g ${CORE[$g]}"$'\n'; done
  for i in "${!FAN[@]}"; do RESULTS+="R $i $([[ $i == "$k" ]] && echo max || echo floor) $(rpm "${FAN[$i]}")"$'\n'; done
}

echo "Baseline (all tested fans to FLOOR, 20s settle):"
for i in "${!PWM[@]}"; do set_manual "${PWM[$i]}" "$FLOOR"; done; sleep 20
read_temps; for g in $GPUS; do printf "  GPU%s=%sC" "$g" "${CORE[$g]}"; done; echo
for k in "${!PWM[@]}"; do phase "$k"; done

echo; echo "================ RESULT ================"
ZONESTR=$(for i in "${!PWM[@]}"; do echo "$i ${LBL[$i]} ${GPU[$i]}"; done)
set +e
python3 - "$ZONESTR" "$RESULTS" <<'PY'
import sys, statistics
zones = {int(i): (p, int(g)) for i, p, g in (l.split() for l in sys.argv[1].splitlines() if l.strip())}
end, rpm = {}, {}
for l in sys.argv[2].splitlines():
    f = l.split()
    if f and f[0] == "T": end[(int(f[1]), int(f[2]))] = int(f[3])
    elif f and f[0] == "R": rpm.setdefault(int(f[1]), {})[f[2]] = int(f[3])
gpus = sorted({g for _, g in end})
mismatch = inconclusive = False
for k, (pwm, cfg_gpu) in sorted(zones.items()):
    # How much colder each GPU ends when THIS fan is maxed vs the other phases.
    delta = {g: end[(k, g)] - statistics.fmean(end[(j, g)] for j in zones if j != k) for g in gpus}
    phys = min(delta, key=delta.get)
    r = rpm.get(k, {}); lo, hi = r.get("floor", 0), r.get("max", 0)
    if hi == 0:
        fan = "NO TACH at MAX (dead/unplugged, tach not wired e.g. splitter's non-tach leg, or zone tach null)"
    elif hi < lo * 1.15 + 100:
        fan = f"NO SPEED CHANGE {lo}->{hi} rpm (on a SATA/molex-powered PWM hub? stalled?)"
    else:
        fan = f"responds {lo}->{hi} rpm"
    print(f"{pwm}: fan {fan}")
    print("   end-temp delta when maxed: " + ", ".join(f"GPU{g} {d:+.1f}C" for g, d in delta.items()))
    if delta[phys] > -1:
        print(f"   INCONCLUSIVE: no GPU cooled >=1C when {pwm} was maxed (config says GPU{cfg_gpu})")
        inconclusive = True
    elif phys == cfg_gpu:
        print(f"   physically cools GPU{phys}  == config  -> OK")
    else:
        print(f"   physically cools GPU{phys}  != config GPU{cfg_gpu}  -> MISMATCH")
        mismatch = True
print()
if mismatch:
    print(">> Fix needed: set each zone's \"pwm\" in gpu-fan-control.config.json to the header that\n"
          "   physically cools its GPU (see above), then: sudo systemctl restart gpu-fan-control")
if inconclusive:
    print(">> Check the flagged fan(s): spinning? tach wired? powered (splitter SATA/molex)? Then re-run\n"
          "   with the GPUs idle (SETTLE=90 for a longer settle).")
if not (mismatch or inconclusive):
    print(">> Config zones match the physical wiring.")
sys.exit(1 if (mismatch or inconclusive) else 0)
PY
rc=$?
set -e
echo "======================================="
exit $rc

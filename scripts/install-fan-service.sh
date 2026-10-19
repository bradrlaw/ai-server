#!/usr/bin/env bash
# Step 3 of GPU fan control: install + start the systemd service.
# Prereqs: setup-fan-sensors.sh done (nct6775 loaded), and the 'pwm' fields in
# gpu-fan-control.config.json set to the correct channels (via identify-fan.sh).
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-fan-service.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

SRC=/srv/ai/scripts
UNIT=/etc/systemd/system/gpu-fan-control.service

# sanity: nct6775 present?
have=0; for h in /sys/class/hwmon/hwmon*; do
  [[ "$(cat "$h/name" 2>/dev/null)" == nct67* ]] && have=1; done
[[ $have -eq 1 ]] || { echo "nct6775 not loaded. Run setup-fan-sensors.sh first."; exit 1; }

# validate config JSON before installing
python3 -c "import json,sys; json.load(open('$SRC/gpu-fan-control.config.json'))" \
  || { echo "config JSON invalid"; exit 1; }

# reject unconfigured pwm placeholders and verify each channel path exists;
# check each driven zone's stall-alarm tach (gpu-fan-control.py _setup_tach)
python3 - "$SRC/gpu-fan-control.config.json" <<'PY' || { echo "Fix the config, then re-run."; exit 1; }
import json, os, re, sys
cfg = json.load(open(sys.argv[1]))
def find_hwmon(name):
    base = "/sys/class/hwmon"
    for h in sorted(os.listdir(base)):
        try:
            if open(os.path.join(base, h, "name")).read().strip() == name:
                return os.path.join(base, h)
        except OSError:
            pass
    return None
bad = False
owner = {}
for z in cfg["zones"]:
    pwm = z.get("pwm") or ""
    if not pwm and z.get("monitor_only"):
        continue
    key = (z.get("hwmon_name", "nct6775"), pwm)
    if pwm and key in owner:
        print(f"  ERROR: zones '{owner[key]}' and '{z['name']}' both use {pwm}: one header per zone."); bad = True
        continue
    owner[key] = z["name"]
    if "REPLACE_ME" in pwm or not pwm:
        print(f"  ERROR: zone '{z['name']}' pwm not set (still '{pwm}'). "
              f"Run identify-fan.sh and edit the config.")
        bad = True
        continue
    hw = find_hwmon(z.get("hwmon_name", "nct6775"))
    if hw is None:
        print(f"  ERROR: hwmon '{z.get('hwmon_name')}' not found."); bad = True; continue
    p = os.path.join(hw, pwm)
    if not os.path.exists(p):
        print(f"  ERROR: zone '{z['name']}' channel {p} does not exist."); bad = True
        continue
    if z.get("monitor_only"):
        continue
    sr = z.get("stall_rpm", 300)
    if isinstance(sr, bool) or not isinstance(sr, int) or not 0 <= sr <= 20000:
        print(f"  ERROR: zone '{z['name']}' stall_rpm={sr!r}: want an integer 0-20000."); bad = True
        continue
    tach = z["tach"] if "tach" in z else f"fan{pwm[3:]}_input"
    if tach is None:
        print(f"  zone '{z['name']}': {pwm}, tach null -> stall alarm off")
        continue
    if not isinstance(tach, str) or not re.fullmatch(r"fan\d+_input", tach):
        print(f"  ERROR: zone '{z['name']}' tach={tach!r}: want \"fanN_input\" or null."); bad = True
        continue
    tp = os.path.join(hw, tach)
    if not os.path.exists(tp):
        if "tach" in z:
            print(f"  ERROR: zone '{z['name']}' tach {tp} does not exist."); bad = True
        else:
            print(f"  NOTE: zone '{z['name']}': no {tach} for {pwm} -> stall alarm off "
                  f"(set \"tach\": \"fanM_input\", or null to make that explicit)")
        continue
    try:
        rpm = int(open(tp).read().strip())
    except (OSError, ValueError):
        rpm = None
    print(f"  zone '{z['name']}': {pwm}, stall alarm on {tach} (< {sr} rpm), now {rpm} rpm")
    if rpm is not None and rpm < sr:
        print(f"  WARNING: {tach} reads {rpm} rpm now: the daemon will hold this zone at 100% "
              f"as stalled unless the fan spins up. No tach wire (or the non-tach leg of a "
              f"Y-splitter)? Set \"tach\": null for this zone.")
pl = cfg.get("power_limits") or {}
if not isinstance(pl, dict):
    print("  ERROR: power_limits must be an object {gpu_index: watts}."); bad = True
else:
    for idx, watts in pl.items():
        try:
            i, w = int(idx), int(watts)
        except (TypeError, ValueError):
            print(f"  ERROR: power_limits['{idx}']={watts!r} not integer."); bad = True; continue
        if not (50 <= w <= 400):
            print(f"  ERROR: power_limits GPU{i}={w}W out of sane range 50-400W."); bad = True
    if pl:
        print(f"  power_limits validated OK: {pl}")
if bad:
    sys.exit(1)
print("  config zones validated OK")
PY

echo "Installing unit -> $UNIT"
cp "$SRC/gpu-fan-control.service" "$UNIT"
systemctl daemon-reload
systemctl enable gpu-fan-control.service
systemctl restart gpu-fan-control.service
sleep 3
systemctl --no-pager --full status gpu-fan-control.service | head -20
echo
ls -l /run/gpu-fan-control/state.json 2>/dev/null \
  || echo "NOTE: /run/gpu-fan-control/state.json not there yet (written every tick)"
echo "Live logs:  journalctl -u gpu-fan-control -f"

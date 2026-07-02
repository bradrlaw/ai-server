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

# reject unconfigured pwm placeholders and verify each channel path exists
python3 - "$SRC/gpu-fan-control.config.json" <<'PY'
import json, os, sys
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
for z in cfg["zones"]:
    pwm = z.get("pwm", "")
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
[[ $? -eq 0 ]] || { echo "Fix the config, then re-run."; exit 1; }

echo "Installing unit -> $UNIT"
cp "$SRC/gpu-fan-control.service" "$UNIT"
systemctl daemon-reload
systemctl enable gpu-fan-control.service
systemctl restart gpu-fan-control.service
sleep 3
systemctl --no-pager --full status gpu-fan-control.service | head -20
echo
echo "Live logs:  journalctl -u gpu-fan-control -f"

#!/usr/bin/env bash
# Step 1 of GPU fan control: bring up the motherboard Super-I/O (Nuvoton nct6775)
# so its 4-pin PWM fan headers become controllable via /sys/class/hwmon.
#
# Board: MSI X99A XPOWER GAMING TITANIUM (MS-7A21) -> Nuvoton NCT679x.
# X99 boards usually need `acpi_enforce_resources=lax` before nct6775 will bind,
# because ACPI reserves the Super-I/O I/O region.
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/setup-fan-sensors.sh
# Idempotent. If a reboot is needed it will tell you, then re-run after reboot.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

CHIP=nct6775

echo "== 1. Install lm-sensors =="
if ! command -v sensors >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y lm-sensors
else
  echo "   lm-sensors already installed."
fi

have_nct() { for h in /sys/class/hwmon/hwmon*; do
  [[ "$(cat "$h/name" 2>/dev/null)" == nct67* ]] && return 0; done; return 1; }

echo "== 2. Try to load $CHIP =="
modprobe "$CHIP" 2>/dev/null || true
sleep 1

if have_nct; then
  echo "   OK: nct6775 hwmon present."
else
  echo "   nct6775 did not bind. Enabling acpi_enforce_resources=lax (needed on X99)."
  GRUBF=/etc/default/grub
  cp -n "$GRUBF" "$GRUBF.bak.$(date +%Y%m%d)" || true
  if ! grep -q 'acpi_enforce_resources=lax' "$GRUBF"; then
    # append the arg inside GRUB_CMDLINE_LINUX_DEFAULT="..."
    sed -i 's/^\(GRUB_CMDLINE_LINUX_DEFAULT="\)\(.*\)"/\1\2 acpi_enforce_resources=lax"/' "$GRUBF"
    sed -i 's/  */ /g; s/=" /="/' "$GRUBF"   # tidy stray spaces
    echo "   Updated: $(grep ^GRUB_CMDLINE_LINUX_DEFAULT "$GRUBF")"
    update-grub
  else
    echo "   acpi_enforce_resources=lax already in GRUB."
  fi
  # persist module autoload for after reboot
  echo "$CHIP" > /etc/modules-load.d/nct6775.conf
  echo
  echo ">>> REBOOT REQUIRED, then re-run this script:  sudo reboot"
  exit 0
fi

echo "== 3. Persist module autoload =="
echo "$CHIP" > /etc/modules-load.d/nct6775.conf
echo "   /etc/modules-load.d/nct6775.conf -> $CHIP"

echo "== 4. sensors output =="
sensors "${CHIP}-"* 2>/dev/null || sensors || true

echo
echo "== 5. PWM channels exposed =="
for h in /sys/class/hwmon/hwmon*; do
  [[ "$(cat "$h/name" 2>/dev/null)" == nct67* ]] || continue
  echo "   nct6775 at: $h"
  for p in "$h"/pwm[0-9]; do
    [[ -e "$p" ]] || continue
    en="${p}_enable"
    printf "     %s  pwm=%s enable=%s\n" "$(basename "$p")" \
      "$(cat "$p" 2>/dev/null)" "$(cat "$en" 2>/dev/null || echo -)"
  done
  for f in "$h"/fan[0-9]_input; do
    [[ -e "$f" ]] && printf "     %s = %s RPM\n" "$(basename "$f")" "$(cat "$f")"
  done
done

echo
echo "DONE. Next: identify which pwmN drives the GPU shroud fans (see"
echo "scripts/identify-fan.sh), then install the GPU-temp control daemon."

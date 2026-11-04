#!/usr/bin/env bash
# Post-boot verification after cloning the boot NVMe / moving GPUs. Read-only; runs
# as brad (no sudo). Prints PASS/WARN/FAIL per check and a summary.
#
# Usage: scripts/verify-boot-migration.sh [--gpus-only] [--no-services]
# Exit:  0 = no FAIL   1 = FAIL
#
# Expected GPU layout (AGENTS.md): idx0 = Titan X, idx1/idx2 = Tesla V100, and the
# power caps in scripts/gpu-fan-control.config.json. Gen/width are only meaningful
# under load; idle cards drop the link speed (a WARN, not a FAIL).
set -u
ROOT=/srv/ai
CFG=$ROOT/scripts/gpu-fan-control.config.json
GPUS_ONLY=0; NO_SVC=0
for a in "$@"; do
  case $a in --gpus-only) GPUS_ONLY=1 ;; --no-services) NO_SVC=1 ;; *) echo "usage: $0 [--gpus-only] [--no-services]"; exit 2 ;; esac
done
export CUDA_DEVICE_ORDER=PCI_BUS_ID
pass=0; warn=0; fail=0
ok()   { echo "PASS  $*"; pass=$((pass+1)); }
wr()   { echo "WARN  $*"; warn=$((warn+1)); }
bad()  { echo "FAIL  $*"; fail=$((fail+1)); }
hdr()  { echo; echo "== $* =="; }

if [ $GPUS_ONLY -eq 0 ]; then
hdr "Boot disk and filesystems"
rootsrc=$(findmnt -no SOURCE /); rootuuid=$(findmnt -no UUID /)
echo "root: $rootsrc (UUID $rootuuid)"
fsuuid=$(awk '$1!~/^#/ && $2=="/"{print $1}' /etc/fstab | sed 's#.*/##')
[ "$rootuuid" = "$fsuuid" ] && ok "/ matches its fstab UUID" || bad "/ ($rootuuid) differs from fstab ($fsuuid)"
dups=$(lsblk -nr -o UUID | awk 'NF' | sort | uniq -d)
[ -z "$dups" ] && ok "no duplicate filesystem UUIDs (only one clone attached)" || bad "duplicate UUIDs (old Intel disk still attached?): $dups"
pdups=$(lsblk -nr -o PARTUUID | awk 'NF' | sort | uniq -d)
[ -z "$pdups" ] && ok "no duplicate PARTUUIDs" || bad "duplicate PARTUUIDs: $pdups"
case $rootsrc in /dev/nvme*) ok "root is on NVMe";; *) wr "root is not on NVMe: $rootsrc";; esac
findmnt -n /boot/efi >/dev/null && ok "/boot/efi mounted" || bad "/boot/efi not mounted"
swapon --show=NAME --noheadings | grep -q . && ok "swap active: $(swapon --show=NAME --noheadings | tr '\n' ' ')" || wr "no swap active"
rootdisk=$(lsblk -no PKNAME "$rootsrc" 2>/dev/null | head -1)
echo "root disk: ${rootdisk:-?}"; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT "/dev/$rootdisk" 2>/dev/null
used=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "$used" -lt 90 ] && ok "root usage ${used}%" || wr "root usage ${used}%"
dmesg 2>/dev/null | grep -iE 'nvme.*(error|timeout|reset)|ext4-fs error' | head -5 | sed 's/^/      dmesg: /' | grep . && wr "NVMe/ext4 errors in dmesg (above)" || ok "no NVMe/ext4 errors in dmesg (or dmesg restricted)"

hdr "EFI boot entries"
if command -v efibootmgr >/dev/null && ls /sys/firmware/efi >/dev/null 2>&1; then
  espuuid=$(lsblk -no PARTUUID "$(findmnt -no SOURCE /boot/efi)")
  efibootmgr 2>/dev/null | grep -E '^Boot(Current|Order)' 
  n=$(efibootmgr -v 2>/dev/null | grep -ci "$espuuid")
  [ "$n" -ge 1 ] && ok "$n boot entries point at this ESP ($espuuid)" || bad "no boot entry references ESP PARTUUID $espuuid"
  efibootmgr -v 2>/dev/null | grep -qi 'Windows Boot Manager' && ok "Windows Boot Manager entry present (still test-boot Windows by hand)" || wr "no Windows Boot Manager entry"
else
  wr "UEFI/efibootmgr not available"
fi

hdr "NVMe health and temperature"
for d in /sys/class/nvme/nvme*; do
  [ -d "$d" ] || continue
  m=$(cat "$d/model" 2>/dev/null); t=""
  for h in "$d"/hwmon*/temp1_input; do [ -r "$h" ] && t=$(( $(cat "$h") / 1000 )); done
  echo "$(basename "$d"): ${m:-?}  temp=${t:-?}C  state=$(cat "$d/state" 2>/dev/null)"
  [ -n "$t" ] && { [ "$t" -lt 70 ] && ok "$(basename "$d") temp ${t}C" || wr "$(basename "$d") temp ${t}C is high"; }
done
fi

hdr "GPUs (CUDA_DEVICE_ORDER=PCI_BUS_ID)"
if ! command -v nvidia-smi >/dev/null; then bad "nvidia-smi missing"; else
  nvidia-smi --query-gpu=index,name,uuid,pci.bus_id,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,power.limit,utilization.gpu --format=csv,noheader
  mapfile -t rows < <(nvidia-smi --query-gpu=index,name,pcie.link.width.current,power.limit --format=csv,noheader,nounits)
  [ "${#rows[@]}" -eq 3 ] && ok "3 GPUs enumerated" || bad "expected 3 GPUs, found ${#rows[@]}"
  for r in "${rows[@]}"; do
    IFS=',' read -r idx name width plim <<<"$r"; idx=${idx// /}; name=${name# }; width=${width// /}; plim=${plim%%.*}; plim=${plim// /}
    case $idx in
      0) case $name in *TITAN*|*Titan*) ok "idx0 is the Titan X";; *) bad "idx0 is '$name', expected Titan X";; esac ;;
      1|2) case $name in *V100*) ok "idx$idx is a V100";; *) bad "idx$idx is '$name', expected V100";; esac
           [ "$width" -ge 16 ] && ok "idx$idx link width x$width" || wr "idx$idx link width x$width (x8 at idle/shared lanes; re-check under load)" ;;
    esac
    want=$(python3 -c "import json;print(json.load(open('$CFG')).get('power_limits',{}).get('$idx',''))" 2>/dev/null)
    [ -n "$want" ] && { [ "$plim" = "$want" ] && ok "idx$idx power cap ${plim}W matches config" || wr "idx$idx power cap ${plim}W, config says ${want}W (fan service re-applies within 30 s)"; }
  done
  echo "      Fan pairing and 225 W cap assume idx2 = the dual-fan-shroud V100 (UUID check):"
  nvidia-smi -L
  echo "      If the cards swapped slots, verify idx<->fan pairing in gpu-fan-control.config.json before loading models."
fi

if [ $NO_SVC -eq 0 ] && [ $GPUS_ONLY -eq 0 ]; then
hdr "Services"
for u in llama-swap gpu-fan-control server-status; do
  systemctl is-active --quiet "$u" && ok "$u active" || bad "$u not active"
done
failed=$(systemctl --failed --no-legend 2>/dev/null | awk '{print $1}' | tr '\n' ' ')
[ -z "$failed" ] && ok "no failed systemd units" || wr "failed units: $failed"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  for c in litellm open-webui mcpo searxng hermes openclaw; do
    s=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c" 2>/dev/null)
    case $s in running/healthy|running/-) ok "container $c $s";; "") wr "container $c not found";; *) bad "container $c $s";; esac
  done
else wr "docker not reachable"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 127.0.0.1:9090/running); [ "$code" = 200 ] && ok "llama-swap API 200" || bad "llama-swap API $code"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 127.0.0.1:9095/status.json); [ "$code" = 200 ] && ok "status.json 200" || bad "status.json $code"
mountpoint -q /srv/ai/storage-bulk && ok "storage-bulk mounted" || wr "storage-bulk not mounted"
mountpoint -q /srv/ai/storage && ok "encrypted storage unlocked" || wr "encrypted storage locked (expected until you unlock: scripts/storage-crypt.sh unlock)"
echo "      Time sync: $(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
fi

hdr "Fans (gpu-fan-control state)"
python3 - <<'PY' | tee /tmp/.vbm-fan.$$
import json, time, sys
try:
    s = json.load(open("/run/gpu-fan-control/state.json"))
except Exception as e:
    print(f"FAIL  fan daemon state unreadable ({e}): is gpu-fan-control running?"); sys.exit(0)
age = time.time() - s.get("ts", 0)
print(("PASS" if age < 30 else "FAIL") + f"  fan state age {age:.0f}s")
if s.get("fault"): print(f"FAIL  daemon fault: {s['fault']}")
for z in s.get("zones", []):
    if z.get("monitor_only"):
        print(f"PASS  {z['name']}: self-cooled (monitor only)"); continue
    rpm = z.get("rpm")
    if z.get("stalled") or rpm == 0:
        print(f"FAIL  {z['name']}: fan STALLED / 0 rpm (check the connector)")
    else:
        print(f"PASS  {z['name']}: {rpm} rpm at {z.get('duty')}%")
PY
while read -r l; do case $l in PASS*) pass=$((pass+1));; FAIL*) fail=$((fail+1));; esac; done < /tmp/.vbm-fan.$$; rm -f /tmp/.vbm-fan.$$
echo; echo "Summary: $pass PASS, $warn WARN, $fail FAIL"
echo "Manual: boot into Windows once from the new disk; confirm UEFI boot order; re-run this with GPUs under load."
[ $fail -eq 0 ]

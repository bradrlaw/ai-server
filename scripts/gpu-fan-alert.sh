# Login banner: shout if the fan daemon reports a stalled fan, fault, stale state or
# an active power cap. Installed to /etc/profile.d by install-fan-service.sh.
case $- in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac
_f=/run/gpu-fan-control/state.json
if [ ! -r "$_f" ]; then
  printf '\n\033[1;37;41m !!! gpu-fan-control is NOT running (no %s) - GPUs may be uncooled !!! \033[0m\n\n' "$_f"
elif command -v python3 >/dev/null 2>&1; then
  python3 - "$_f" <<'PY'
import json, sys, time
try:
    s = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"\n\033[1;37;41m !!! cannot read fan state: {e} !!! \033[0m\n"); sys.exit(0)
msgs = []
if time.time() - s.get("ts", 0) > 60:
    msgs.append("fan daemon state is STALE (daemon hung or stopped)")
if s.get("fault"):
    msgs.append(f"fan daemon fault: {s['fault']}")
for z in s.get("zones", []):
    if z.get("stalled") or z.get("state") == "stall":
        msgs.append(f"fan STALLED: {z.get('name')} ({z.get('rpm')} rpm)")
p = s.get("protect") or {}
if p.get("capped_gpus"):
    msgs.append(f"GPU(s) {p['capped_gpus']} power-capped to {p.get('stall_cap_watts')} W")
for m in msgs:
    print(f"\033[1;37;41m !!! {m} !!! \033[0m")
if msgs:
    print()
PY
fi
unset _f

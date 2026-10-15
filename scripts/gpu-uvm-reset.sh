#!/usr/bin/env bash
# Recover from a poisoned NVIDIA UVM state: every NEW CUDA context fails
# ("ggml_cuda_init: failed to initialize CUDA: unknown error", torch sees 0 devices)
# after a llama.cpp/fork GPU process segfaulted or was SIGKILLed mid-CUDA. Processes
# that already had a context keep working, and llama-swap silently falls back to CPU
# for any model it (re)loads. This reloads nvidia_uvm without a reboot:
#
#   1. Shows the GPU services that are running (or crash-looping) and any other
#      /dev/nvidia-uvm holders, then asks to confirm. Everything on the GPUs is
#      interrupted (renders, requests, training).
#   2. Re-reads the services, stops every GPU service (always-on AND on-demand) and
#      checks that they really stopped. If one didn't, it leaves nvidia_uvm alone.
#   3. Kills whatever still holds /dev/nvidia-uvm (e.g. an `nvidia-smi dmon` in a shell).
#   4. rmmod + modprobe nvidia_uvm. rmmod is retried: a status poll can hold it briefly.
#   5. Checks that a NEW CUDA context sees every GPU (llama-server --list-devices).
#   6. Restarts the always-on services that were running and checks that they came up.
#      This also runs after a failed step or an interrupt (EXIT trap), and it loads
#      nvidia_uvm first if it's missing, so llama-swap isn't left down. While it runs,
#      Ctrl-C, TERM and HUP are ignored (Ctrl-\ still kills it outright). On-demand
#      services stay stopped (owner decision 2026-10-15); the summary lists the ones
#      to start again from the status page.
#
# Usage: sudo scripts/gpu-uvm-reset.sh [-y|--yes] [-n|--dry-run]
#   -y, --yes      skip the confirmation (required when stdin isn't a terminal)
#   -n, --dry-run  show what would be stopped and killed, change nothing. Works without
#                  root, but then other users' processes are invisible.
# Exit status: 0 reloaded, every GPU visible and the services back up; 1 failed,
# unverified or aborted; 2 usage, not root, or another reset is running; 129/130/143
# interrupted by HUP/INT/TERM (after the always-on services were put back).
set -uo pipefail   # no -e: every step is checked so the restart in the EXIT trap always runs

# Enabled at boot. Restarted after the reload if they were running (llama-swap first).
ALWAYS_ON=(llama-swap comfyui-open comfyui-secure ai-toolkit-ui)
# Disabled at boot, started by hand from the status page. Stopped and left stopped.
ON_DEMAND=(comfyui-qwen fooocus swarmui invokeai comfyui)
UVM_DEV=/dev/nvidia-uvm
# Overridable for tests; sudo's env_reset drops both in real runs.
LLAMA_SERVER=${LLAMA_SERVER:-/srv/ai/src/llama.cpp/build/bin/llama-server}
LOCK=${GPU_UVM_RESET_LOCK:-/run/gpu-uvm-reset.lock}

YES=0 DRY=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    -n|--dry-run) DRY=1 ;;
    -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done
if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "Run as root:  sudo $0    (or: $0 --dry-run to just look)" >&2
  exit 2
fi
if ((DRY == 0)); then
  # Held until exit. Only root can create files in /run. The file is 0600 because flock
  # works on a read-only descriptor too: anyone who can open it can hold the lock.
  old_umask=$(umask)
  umask 077
  if ! exec 9>"$LOCK"; then
    echo "Can't open the lock file $LOCK" >&2
    exit 2
  fi
  umask "$old_umask"
  chmod 600 "$LOCK"   # in case an older file is looser
  if ! flock -n 9; then
    echo "Another gpu-uvm-reset is already running." >&2
    exit 2
  fi
fi

is_always_on() {
  local u
  for u in "${ALWAYS_ON[@]}"; do [ "$u" = "$1" ] && return 0; done
  return 1
}

state_of() { systemctl show -p ActiveState --value "$1.service" 2>/dev/null; }

uvm_loaded() { lsmod | awk '$1 == "nvidia_uvm" {found = 1} END {exit !found}'; }

# KNOWN: the installed GPU units. RUNNING: the ones up or coming up (activating covers
# a crash loop waiting for its automatic restart). STATE: ActiveState per unit.
KNOWN=() RUNNING=()
declare -A STATE=()
snapshot() {
  local u
  KNOWN=() RUNNING=() STATE=()
  for u in "${ALWAYS_ON[@]}" "${ON_DEMAND[@]}"; do
    [ "$(systemctl show -p LoadState --value "$u.service" 2>/dev/null)" = loaded ] || continue
    KNOWN+=("$u")
    STATE[$u]=$(state_of "$u")
    case "${STATE[$u]}" in active|activating|reloading) RUNNING+=("$u") ;; esac
  done
}

# PIDs with /dev/nvidia-uvm open or mapped (never PID 1 or this script).
holders() {
  fuser "$UVM_DEV" 2>/dev/null | tr -s ' \t' '\n' | sed -n 's/^\([0-9][0-9]*\).*/\1/p' |
    awk -v self="$$" '$1 != 1 && $1 != self' | sort -un
}

describe() {
  (($#)) || return 0
  ps -o pid=,user=,unit=,args= -p "$(IFS=,; echo "$*")" 2>/dev/null | cut -c1-150 | sed 's/^/    /'
}

kill_holders() {
  local -a pids
  local i
  mapfile -t pids < <(holders)
  if ((${#pids[@]} == 0)); then
    echo "  nothing else holds $UVM_DEV"
    return 0
  fi
  echo "  still holding $UVM_DEV, sending SIGTERM:"
  describe "${pids[@]}"
  kill -TERM "${pids[@]}" 2>/dev/null
  for i in 1 2 3 4 5; do
    sleep 1
    mapfile -t pids < <(holders)
    ((${#pids[@]})) || return 0
  done
  echo "  still alive after 5 s, sending SIGKILL: ${pids[*]}"
  kill -KILL "${pids[@]}" 2>/dev/null
  sleep 1
}

reload_uvm() {
  local attempt err
  local -a pids
  if uvm_loaded; then
    for attempt in 1 2 3; do
      err=$(rmmod nvidia_uvm 2>&1) && break
      echo "  rmmod try $attempt/3 failed: ${err:-no message}"
      if ((attempt < 3)); then
        sleep 2
        kill_holders
      fi
    done
    if uvm_loaded; then
      echo "!! nvidia_uvm is still loaded (refcnt $(cat /sys/module/nvidia_uvm/refcnt 2>/dev/null || echo '?')). Holders now:"
      mapfile -t pids < <(holders)
      if ((${#pids[@]})); then describe "${pids[@]}"; else echo "    none visible to fuser"; fi
      echo "   Stop or kill those, then re-run this script. Otherwise reboot."
      return 1
    fi
  else
    echo "  nvidia_uvm wasn't loaded (an earlier reset stopped halfway?), loading it"
  fi
  if ! err=$(modprobe nvidia_uvm 2>&1) || ! uvm_loaded; then
    echo "!! modprobe nvidia_uvm failed: ${err:-module still not loaded}"
    return 1
  fi
  echo "  nvidia_uvm reloaded"
  return 0   # not echo's status: a closed stdout (SSH gone) must not fail the reload
}

sanity() {
  local want got out owner rc
  out=$(nvidia-smi -L 2>&1)
  rc=$?
  want=$(printf '%s\n' "$out" | grep -c '^GPU ')
  if ((rc != 0 || want == 0)); then
    echo "!! nvidia-smi -L failed (exit $rc, $want GPUs listed):"
    printf '%s\n' "$out" | tail -n 5 | sed 's/^/    /'
    return 1
  fi
  if [ ! -x "$LLAMA_SERVER" ]; then
    echo "!! can't verify: $LLAMA_SERVER is missing, so no new CUDA context was tried."
    echo "   Check by hand: load a model through llama-swap and watch nvidia-smi."
    return 1
  fi
  # Least privilege: the probe doesn't need root, so it runs as the binary's owner.
  owner=$(stat -c %U "$LLAMA_SERVER")
  out=$(runuser -u "$owner" -- env CUDA_DEVICE_ORDER=PCI_BUS_ID timeout 60 "$LLAMA_SERVER" --list-devices 2>&1 9>&-)
  rc=$?
  got=$(printf '%s\n' "$out" | grep -cE '^[[:space:]]+CUDA[0-9]+:')
  if ((rc == 0 && got == want)); then
    echo "  OK: a new CUDA context sees $got/$want GPUs"
    printf '%s\n' "$out" | grep -E '^[[:space:]]+CUDA[0-9]+:'
    return 0
  fi
  if ((rc == 124)); then
    echo "!! the CUDA probe hung for 60 s (it listed $got of $want GPUs):"
  else
    echo "!! a new CUDA context sees $got of $want GPUs (probe exit $rc):"
  fi
  printf '%s\n' "$out" | tail -n 15 | sed 's/^/    /'
  echo "   If new CUDA contexts keep failing, reboot."
  return 1
}

RC=0 RESTORED=0
RESTART=() LEFT=()

on_signal() {  # $1 = 128 + signal number
  trap '' INT TERM HUP
  echo
  echo "!! interrupted, putting the always-on services back" >&2
  RC=1
  exit "$1"
}

restore() {
  ((RESTORED)) && return 0
  # Finish this step whatever happens. Ignored signals are inherited, so a Ctrl-C (sent
  # to the whole foreground process group) can't kill modprobe or systemctl start either.
  trap '' INT TERM HUP
  local u st
  echo "== 5. Restart the always-on services that were running"
  if ! uvm_loaded; then
    echo "  nvidia_uvm isn't loaded, loading it"
    modprobe nvidia_uvm 2>&1 | sed 's/^/    /'
  fi
  if ((${#RESTART[@]} == 0)); then
    echo "  none were running"
  elif ! uvm_loaded; then
    echo "!! nvidia_uvm still isn't loaded, so NOT starting ${RESTART[*]} (no CUDA: CPU fallback or crash loop)."
    echo "   Reboot, or: sudo modprobe nvidia_uvm && sudo systemctl start ${RESTART[*]}"
    RC=1
  else
    for u in "${RESTART[@]}"; do
      if ! systemctl start "$u.service"; then
        echo "!! $u failed to start (journalctl -u $u)"
        RC=1
        continue
      fi
      # start exits 0 when a unit condition skips it (comfyui-secure: storage locked).
      st=$(state_of "$u")
      if [ "$st" = active ]; then
        echo "  started $u"
      elif [ "$(systemctl show -p ConditionResult --value "$u.service" 2>/dev/null)" = no ]; then
        echo "!! $u did not start: a unit condition failed (systemctl status $u)"
        RC=1
      else
        echo "!! $u is $st right after starting (journalctl -u $u)"
        RC=1
      fi
    done
  fi
  if ((${#LEFT[@]})); then
    echo "On-demand services that were running are left STOPPED: ${LEFT[*]}"
    echo "  Start them again from the status page (Services, Start) or: sudo systemctl start ${LEFT[*]}"
  fi
  RESTORED=1
}

do_reset() {
  local u st
  local -a stuck=()
  echo "== 1. Stop the GPU services"
  snapshot   # again: the confirmation may have waited a long time
  for u in "${RUNNING[@]}"; do
    if is_always_on "$u"; then RESTART+=("$u"); else LEFT+=("$u"); fi
  done
  echo "  running now: ${RUNNING[*]:-none}"
  # All installed ones, running or not: a stop also cancels a pending automatic restart.
  if ((${#KNOWN[@]})); then
    systemctl stop "${KNOWN[@]/%/.service}" || echo "  (systemctl stop reported an error, checking the units)"
  fi
  for u in "${KNOWN[@]}"; do
    st=$(state_of "$u")
    case "$st" in inactive|failed) ;; *) stuck+=("$u ($st)") ;; esac
  done
  if ((${#stuck[@]})); then
    echo "!! still not stopped: ${stuck[*]}"
    echo "   Leaving nvidia_uvm alone. See: systemctl status <unit>"
    return 1
  fi

  echo "== 2. Kill the remaining $UVM_DEV holders"
  kill_holders

  echo "== 3. Reload nvidia_uvm"
  reload_uvm || return 1

  echo "== 4. Sanity: a NEW CUDA context must see every GPU"
  sanity
}

# ---- plan --------------------------------------------------------------------------
snapshot
mapfile -t HOLDERS < <(holders)
declare -A UNIT_OF=()
if ((${#HOLDERS[@]})); then
  while read -r pid unit; do
    UNIT_OF[$pid]=$unit
  done < <(ps -o pid=,unit= -p "$(IFS=,; echo "${HOLDERS[*]}")" 2>/dev/null)
fi
OTHERS=()
for pid in "${HOLDERS[@]}"; do
  inside=0
  for u in "${RUNNING[@]}"; do [ "${UNIT_OF[$pid]:-}" = "$u.service" ] && inside=1; done
  ((inside)) || OTHERS+=("$pid")
done

echo "== GPU services running now"
((${#RUNNING[@]})) || echo "  none"
for u in "${RUNNING[@]}"; do
  note=
  [ "${STATE[$u]}" = active ] || note=" (${STATE[$u]})"
  if is_always_on "$u"; then
    printf '  %-15s always-on: stopped, restarted after the reload%s\n' "$u" "$note"
  else
    printf '  %-15s on-demand: stopped and LEFT STOPPED (start it again from the status page)%s\n' "$u" "$note"
  fi
done
echo "== Other processes holding $UVM_DEV (killed)"
if ((${#OTHERS[@]})); then describe "${OTHERS[@]}"; else echo "  none"; fi
[ "$(id -u)" -eq 0 ] || echo "  (not root: other users' processes are invisible here)"
echo "Everything running on the GPUs is interrupted: renders, model requests, training jobs."

if ((DRY)); then
  echo "Dry run: nothing changed."
  exit 0
fi
if ((YES == 0)); then
  if [ -t 0 ]; then
    read -r -p "Proceed? [y/N] " ans || ans=
    case "$ans" in
      [yY]|[yY][eE][sS]) ;;
      *) echo "Aborted, nothing changed."; exit 1 ;;
    esac
  else
    echo "stdin isn't a terminal: re-run with --yes to confirm." >&2
    exit 2
  fi
fi

# ---- reset -------------------------------------------------------------------------
trap restore EXIT
trap '' PIPE   # a closed stdout (`| tee` killed when SSH drops) must not stop the reset
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
trap 'on_signal 129' HUP

do_reset || RC=1
restore
if ((RC == 0)); then
  echo "Done: nvidia_uvm reloaded, new CUDA contexts see every GPU, services restarted."
  echo "llama-swap preloads its models in the background (about a minute)."
else
  echo "Finished with problems, see the !! lines above."
fi
exit "$RC"

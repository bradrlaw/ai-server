#!/usr/bin/env bash
# Read-only checks for a manual OS update / kernel upgrade (ADR-0040). No sudo, changes nothing.
#
#   scripts/os-update-check.sh pre     after `sudo apt update`, before `sudo apt full-upgrade`
#   scripts/os-update-check.sh ready   after the upgrade, before rebooting
#   scripts/os-update-check.sh post    after the reboot
#
# Exit 1 if any FAIL. WARN lines need a human look but don't block.
set -uo pipefail

MODE=${1:-}
EXPECT_GPUS=${EXPECT_GPUS:-3}
FAILS=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
hdr()  { printf '\n== %s\n' "$*"; }

driver_pkgs() { dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 2>/dev/null | awk '$1 ~ /^[hi]i/ && $2 ~ /580-server/ {print $2}'; }

check_auto_updates() {
    hdr "Automatic updates (must stay off)"
    local f=/etc/apt/apt.conf.d/20auto-upgrades
    if grep -q -E '"[1-9]' "$f" 2>/dev/null; then fail "$f enables periodic apt: $(grep -E '"[1-9]' "$f" | tr '\n' ' ')"; else ok "$f is all \"0\""; fi
    local u s
    for u in unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer; do
        s=$(systemctl is-enabled "$u" 2>/dev/null || true)
        case "$s" in disabled|masked|"") ok "$u ${s:-not installed}" ;; *) fail "$u is $s" ;; esac
    done
}

check_holds() {
    hdr "NVIDIA 580-server driver stack held"
    local held pkgs n=0 missing=0 p
    held=$(apt-mark showhold 2>/dev/null)
    pkgs=$(driver_pkgs)
    [[ -n "$pkgs" ]] || { fail "no *580-server* packages installed?"; return; }
    for p in $pkgs; do
        n=$((n + 1))
        grep -qx "$p" <<<"$held" || { fail "not held: $p"; missing=$((missing + 1)); }
    done
    [[ $missing -eq 0 ]] && ok "all $n driver packages held ($(dpkg-query -W -f='${Version}' nvidia-driver-580-server 2>/dev/null))"
}

check_driver_runtime() {
    hdr "Loaded NVIDIA module vs userspace"
    local kmod user n
    kmod=$(sed -n 's/.*Kernel Module *\([0-9.]*\).*/\1/p' /proc/driver/nvidia/version 2>/dev/null)
    user=$(timeout 30 nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    n=$(timeout 30 nvidia-smi -L 2>/dev/null | grep -c '^GPU ')
    [[ -n "$kmod" ]] || { fail "nvidia kernel module not loaded"; return; }
    if [[ -z "$user" ]]; then fail "nvidia-smi failed (module $kmod) - driver/library mismatch?"; return; fi
    [[ "$kmod" == "$user" ]] && ok "module $kmod == nvidia-smi $user" || fail "module $kmod != userspace $user (reboot needed)"
    [[ "$n" -eq "$EXPECT_GPUS" ]] && ok "$n GPUs visible" || fail "$n GPUs visible, expected $EXPECT_GPUS"
}

check_simulation() {
    hdr "Simulated 'apt full-upgrade' (no changes made)"
    local sim chg age
    age=$(( ( $(date +%s) - $(stat -c %Y /var/lib/apt/lists/partial 2>/dev/null || echo 0) ) / 86400 ))
    [[ $age -le 1 ]] && ok "apt lists refreshed ${age}d ago" || warn "apt lists are ${age} days old: run 'sudo apt update' first"
    sim=$(apt-get -s -o Debug::NoLocking=1 full-upgrade 2>&1) || { fail "simulation failed:"; tail -5 <<<"$sim"; return; }
    chg=$(grep -E '^(Inst|Remv|Purg)' <<<"$sim")
    printf '  %s package actions\n' "$(grep -c . <<<"$chg")"
    local x
    x=$(grep -E '580-server' <<<"$chg")
    [[ -n "$x" ]] && { fail "held driver packages would change:"; sed 's/^/          /' <<<"$x"; }
    x=$(grep -E '^Inst (nvidia-driver-|nvidia-dkms-|nvidia-kernel-|libnvidia-compute-|cuda-drivers)' <<<"$chg" | grep -v 580-server)
    [[ -n "$x" ]] && { fail "a different NVIDIA driver would be installed (ADR-0002):"; sed 's/^/          /' <<<"$x"; }
    x=$(grep -E '^Inst cuda-(toolkit-)?13' <<<"$chg")
    [[ -n "$x" ]] && { fail "CUDA 13 toolkit would be installed (drops sm_52/sm_70):"; sed 's/^/          /' <<<"$x"; }
    x=$(grep -E '^(Remv|Purg)' <<<"$chg")
    [[ -n "$x" ]] && { warn "removals:"; sed 's/^/          /' <<<"$x"; }
    local k
    k=$(sed -n 's/^Inst linux-image-\([0-9][^ ]*-generic\) .*/\1/p' <<<"$chg" | sort -V | tail -1)
    [[ -n "$k" ]] && ok "new kernel: $k (running $(uname -r))" || ok "no new kernel"
    local g
    g=$(grep -E '^Inst (dkms|linux-firmware|docker-|containerd|nvidia-container|libnvidia-container|cudnn|libcudnn|cuda-)' <<<"$chg" \
        | awk '{print $2}' | sed 's/linux-firmware-.*/linux-firmware-*/' | sort -u | tr '\n' ' ')
    [[ -n "$g" ]] && warn "GPU/container-relevant updates (review): $g"
    [[ $(df --output=avail -BG / | tail -1 | tr -dc 0-9) -ge 5 ]] && ok "free space on / OK" || fail "less than 5 GB free on /"
}

check_dkms_ready() {
    hdr "NVIDIA DKMS module built for every installed kernel"
    local ver kernels k newest
    ver=$(dpkg-query -W -f='${Version}' nvidia-dkms-580-server 2>/dev/null | cut -d- -f1)
    kernels=$(ls /boot/vmlinuz-*-generic 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V)
    newest=$(tail -1 <<<"$kernels")
    for k in $kernels; do
        if [[ ! -d /usr/src/linux-headers-$k ]]; then
            [[ $k == "$newest" ]] && fail "$k: no headers (linux-headers-$k)" || warn "$k: no headers"
            continue
        fi
        if ls /lib/modules/"$k"/updates/dkms/nvidia.ko* >/dev/null 2>&1 \
           && [[ $(modinfo -k "$k" -F version nvidia 2>/dev/null) == "$ver" ]]; then
            ok "$k: nvidia $ver built"
        elif [[ $k == "$newest" ]]; then
            fail "$k (boots by default): nvidia module missing or not $ver; see 'dkms status' and /var/lib/dkms/nvidia-srv/$ver/build/make.log"
        else
            warn "$k: nvidia module missing"
        fi
    done
    [[ $newest != "$(uname -r)" ]] && ok "next boot: $newest (fallback $(uname -r) stays installed)"
    [[ -f /var/run/reboot-required ]] && ok "reboot required: $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null | cut -c1-120)"
}

check_services() {
    hdr "Services after reboot"
    local u st age
    for u in gpu-fan-control llama-swap docker browser-fetch-mcp; do
        st=$(systemctl is-active "$u" 2>/dev/null)
        [[ $st == active ]] && ok "$u active" || fail "$u is ${st:-unknown}"
    done
    age=$(( $(date +%s) - $(stat -c %Y /run/gpu-fan-control/state.json 2>/dev/null || echo 0) ))
    [[ $age -lt 90 ]] && ok "fan daemon state fresh (${age}s)" || fail "fan daemon state.json stale (${age}s)"
    timeout 30 nvidia-smi --query-gpu=index,name,power.limit --format=csv,noheader 2>/dev/null | sed 's/^/          /'
    local bad
    bad=$(docker ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -v -E ' Up |Exited \(0\)' | grep -v -E '^docker-gh-runner' || true)
    [[ -z "$bad" ]] && ok "all containers up" || warn "containers not up: $(tr '\n' ';' <<<"$bad")"
    mountpoint -q /srv/ai/storage && ok "encrypted storage unlocked" || warn "encrypted storage locked: sudo /srv/ai/scripts/storage-crypt.sh unlock"
}

case "$MODE" in
    pre)   check_auto_updates; check_holds; check_driver_runtime; check_simulation ;;
    ready) check_holds; check_dkms_ready ;;
    post)  printf 'kernel %s\n' "$(uname -r)"; check_auto_updates; check_holds; check_driver_runtime; check_services ;;
    *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

printf '\n'
if [[ $FAILS -gt 0 ]]; then echo "RESULT: $FAILS FAIL(s) - stop and fix before continuing."; exit 1; fi
echo "RESULT: OK ($MODE)"

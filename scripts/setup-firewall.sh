#!/usr/bin/env bash
# Enable the host firewall for the AI server (ADR-0028). Run with sudo.
#
# Policy (ufw: default deny incoming, allow outgoing; routed stays deny):
#   1. Docker containers -> host: ONLY the allowlisted TCP ports (DOCKER_HOST_PORTS);
#      every other port is REJECTed (fails fast instead of hanging). These two rules
#      are inserted at the TOP so nothing broader (e.g. "allow 22/tcp") can override
#      them for container sources.
#   2. SSH 22/tcp from anywhere (anti-lockout; the box sits behind NAT).
#   3. Everything on tailscale0 (the tailnet) + Tailscale's WireGuard UDP port.
#   4. Everything from the LAN: the IPv4 subnet, the on-link IPv6 ULA prefix(es) and
#      link-local (all auto-detected from the default-route interface).
#   5. Docker-PUBLISHED ports: IPv4 publishes are DNATed + forwarded and never hit
#      ufw's INPUT rules, so a managed DOCKER-USER block in /etc/ufw/after.rules
#      - drops NEW connections that arrive on the LAN interface from a non-LAN source;
#      - rejects containers that reach a published port via the host's address (from
#        another Docker network that path is forwarded, not proxied, so rule 1 never
#        sees it).
#      (IPv6 publishes go through docker-proxy on the host and are covered by 1-4.)
#
# Safety: before touching anything it snapshots the ufw config, the chain policies and
# DOCKER-USER, and arms a dead-man switch (transient systemd timer whose rollback is
# retried on failure) that restores the snapshot after CONFIRM_SECS unless you type
# "keep" after checking access from a NEW SSH session. Ctrl-C, a dropped SSH session,
# any error or any FAIL from the post-apply verification rolls back too. Existing
# connections survive. A reboot or power loss inside the window is covered by a
# ufw.service drop-in (ai-fw-ufw-rollback.conf, installed here, inert otherwise): while
# the run's PENDING marker exists it restores the files at boot, before ufw loads them,
# and if that fails ufw loads nothing. Every rollback verifies what it restored (files,
# ufw loaded or not, chain policies, DOCKER-USER) and reports failure loudly.
#
# Idempotent: re-run after changing DOCKER_HOST_PORTS or when the LAN / IPv6 prefix
# changes; stale managed rules (comment "ai-fw:...") are pruned. Rules you added by
# hand without that comment are left alone.
#
# Usage: sudo scripts/setup-firewall.sh             # show plan, apply, confirm
#        scripts/setup-firewall.sh --dry-run        # print the plan only (no root)
# Env:   LAN_IF LAN_CIDR LAN_V6 DOCKER_NET DOCKER_HOST_PORTS TS_IF TS_UDP_PORT
#        CONFIRM_SECS (default 300)
# Verify any time:  sudo scripts/verify-firewall.sh
# Undo a kept run:  sudo /bin/sh /etc/ufw/ai-fw-snapshot/rollback.sh
#
# Error handling: plain errexit + an EXIT trap. Once the dead-man switch is armed,
# ANY exit other than a confirmed "keep" restores the snapshot. (Deliberately no
# ERR trap: with errtrace it also fires inside command substitutions.)
set -euo pipefail

DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1 (see --help)"; exit 2 ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
UFW=/usr/sbin/ufw
AFTER=/etc/ufw/after.rules
SNAP=/etc/ufw/ai-fw-snapshot
RB_LOCK=/run/ai-fw-rollback.lock   # outside $SNAP, which a re-run replaces
UNIT=ai-fw-rollback
DROPIN_SRC=ai-fw-ufw-rollback.conf
DROPIN=/etc/systemd/system/ufw.service.d/ai-fw-rollback.conf
BOOT_ID_FILE=/proc/sys/kernel/random/boot_id
TAG=ai-fw
BEGIN_MARK="# BEGIN ai-fw DOCKER-USER (managed by /srv/ai/scripts/setup-firewall.sh, ADR-0028)"
END_MARK="# END ai-fw DOCKER-USER"

DOCKER_NET=${DOCKER_NET:-172.16.0.0/12}
DOCKER_HOST_PORTS=${DOCKER_HOST_PORTS:-4000,8188,9000,9001,9095,9120,9130}
TS_IF=${TS_IF:-tailscale0}
TS_UDP_PORT=${TS_UDP_PORT:-41641}
SSH_PORT=22
CONFIRM_SECS=${CONFIRM_SECS:-300}

die() { echo "ERROR: $*" >&2; exit 1; }
# waiting = armed; running = firing right now; dead/elapsed/"" = not pending.
timer_state() { systemctl show -p SubState --value "$UNIT.timer" 2>/dev/null || true; }

# --- detect + validate ----------------------------------------------------------
LAN_IF=${LAN_IF:-$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i+1); exit}}')}
[[ -n $LAN_IF ]] || die "no IPv4 default route; set LAN_IF"
LAN_CIDR=${LAN_CIDR:-$(ip -4 route show dev "$LAN_IF" proto kernel scope link | awk 'NR == 1 {print $1}')}
[[ -n $LAN_CIDR ]] || die "could not detect the LAN subnet on $LAN_IF; set LAN_CIDR"
# On-link ULA prefixes (fc00::/7) learned from router advertisements. Global
# prefixes are deliberately NOT trusted.
LAN_V6=${LAN_V6-$(ip -6 route show dev "$LAN_IF" | awk '$1 ~ /^f[cd][0-9a-f]*:.*\/[0-9]+$/ {print $1}' | sort -u | paste -sd' ' -)}

[[ $DOCKER_HOST_PORTS =~ ^[0-9]{1,5}(,[0-9]{1,5}){0,14}$ ]] \
  || die "DOCKER_HOST_PORTS must be a comma list of at most 15 ports (ufw multiport limit)"
[[ $TS_UDP_PORT =~ ^[0-9]{1,5}$ ]] || die "TS_UDP_PORT must be a port number"
[[ $CONFIRM_SECS =~ ^[0-9]+$ ]] && (( CONFIRM_SECS >= 90 )) || die "CONFIRM_SECS must be >= 90"
[[ $LAN_IF =~ ^[A-Za-z0-9._-]+$ && $TS_IF =~ ^[A-Za-z0-9._-]+$ ]] || die "bad interface name"
# shellcheck disable=SC2086  # LAN_V6 is a space-separated list on purpose
python3 - "$LAN_CIDR" "$DOCKER_NET" $LAN_V6 <<'EOF' || die "address validation failed (see above)"
import ipaddress, sys
lan, dock, *v6 = sys.argv[1:]
n = ipaddress.ip_network(lan)
d = ipaddress.ip_network(dock)
ula = ipaddress.ip_network("fc00::/7")
if not (n.version == 4 and n.is_private):
    sys.exit(f"LAN_CIDR {lan}: must be a private IPv4 subnet")
if not (d.version == 4 and d.is_private):
    sys.exit(f"DOCKER_NET {dock}: must be a private IPv4 range")
if n.overlaps(d):
    sys.exit(f"LAN_CIDR {lan} overlaps DOCKER_NET {dock}")
for p in v6:
    q = ipaddress.ip_network(p)
    if not (q.version == 6 and q.subnet_of(ula)):
        sys.exit(f"LAN_V6 {p}: only ULA (fc00::/7) prefixes are trusted")
EOF

# --- desired state --------------------------------------------------------------
# General rules: "spec|label". Appended; ufw skips exact duplicates and updates the
# comment in place when only the tag changed.
RULES=(
  "allow $SSH_PORT/tcp|ssh anti-lockout"
  "allow in on $TS_IF|tailnet"
  "allow $TS_UDP_PORT/udp|tailscale direct udp"
  "allow from $LAN_CIDR|lan ipv4"
)
for p in $LAN_V6; do RULES+=("allow from $p|lan ipv6 ula"); done
RULES+=("allow in on $LAN_IF from fe80::/10|lan ipv6 link-local")
HASH=$(printf '%s\n' "${RULES[@]}" | sha256sum | cut -c1-8)

# Container -> host rules, kept at the top of the rule list (allow first, then reject).
# verify-firewall.sh recomputes this tag with the same recipe: keep them in sync.
DOCKER_ALLOW="allow proto tcp from $DOCKER_NET to any port $DOCKER_HOST_PORTS"
DOCKER_REJECT="reject from $DOCKER_NET"
DHASH=$(printf '%s\n%s\n' "$DOCKER_ALLOW" "$DOCKER_REJECT" | sha256sum | cut -c1-8)
DOCKER_ALLOW_TAG="$TAG:docker-$DHASH allowlist"
DOCKER_REJECT_TAG="$TAG:docker-$DHASH reject other ports"
# While that pair is replaced, these keep containers off every host port (fail
# closed). Their rule text must differ from DOCKER_REJECT: adding an identical rule
# with another comment only re-tags it in place, it doesn't add or move anything.
TRANSIENTS=("reject proto tcp from $DOCKER_NET" "reject proto udp from $DOCKER_NET")
TRANSIENT_TAG="$TAG:transient container lockout during update"

DOCKER_USER_BLOCK="$BEGIN_MARK
# Docker-published IPv4 ports are DNATed and forwarded, so ufw's INPUT rules never
# see them.
# 1. Drop NEW connections that arrive on the LAN interface from outside the LAN
#    (tailnet traffic arrives on $TS_IF and is not affected).
# 2. Reject containers reaching a published port via the host's address: from another
#    Docker network that is DNATed + forwarded instead of going through docker-proxy
#    and INPUT. ORIGINAL direction only, so replies to LAN/tailnet clients of a
#    published port (their source is the container) still pass.
*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -i $LAN_IF ! -s $LAN_CIDR -m conntrack --ctstate NEW -m limit --limit 3/min --limit-burst 10 -m comment --comment \"ai-fw log non-LAN to published ports\" -j LOG --log-prefix \"[AI-FW DOCKER BLOCK] \"
-A DOCKER-USER -i $LAN_IF ! -s $LAN_CIDR -m conntrack --ctstate NEW -m comment --comment \"ai-fw drop non-LAN to published ports\" -j DROP
-A DOCKER-USER -s $DOCKER_NET -m conntrack --ctstate DNAT --ctdir ORIGINAL -m limit --limit 3/min --limit-burst 10 -m comment --comment \"ai-fw log containers to published ports\" -j LOG --log-prefix \"[AI-FW DOCKER XNET] \"
-A DOCKER-USER -s $DOCKER_NET -p tcp -m conntrack --ctstate DNAT --ctdir ORIGINAL -m comment --comment \"ai-fw reject containers to published ports\" -j REJECT --reject-with tcp-reset
-A DOCKER-USER -s $DOCKER_NET -m conntrack --ctstate DNAT --ctdir ORIGINAL -m comment --comment \"ai-fw reject containers to published ports\" -j REJECT --reject-with icmp-port-unreachable
-A DOCKER-USER -j RETURN
COMMIT
$END_MARK"

show_plan() {
  echo "LAN: $LAN_IF  $LAN_CIDR  ${LAN_V6:-<no ULA prefix>}  + fe80::/10"
  echo "Tailnet: $TS_IF (+ $TS_UDP_PORT/udp)   Docker: $DOCKER_NET -> tcp $DOCKER_HOST_PORTS"
  echo
  echo "ufw rules (in evaluation order):"
  echo "  [top] $DOCKER_ALLOW   # $DOCKER_ALLOW_TAG"
  echo "  [top] $DOCKER_REJECT   # $DOCKER_REJECT_TAG"
  local r
  for r in "${RULES[@]}"; do echo "        ${r%%|*}   # $TAG:$HASH ${r#*|}"; done
  echo "  default: deny incoming, allow outgoing (routed unchanged: deny)"
  echo
  echo "$AFTER managed block:"
  sed 's/^/  /' <<<"$DOCKER_USER_BLOCK"
  echo
  echo "Safety: ${CONFIRM_SECS}s dead-man switch (retried on failure) + a ufw.service drop-in that rolls back at boot while unconfirmed"
}

if (( DRY )); then
  echo "DRY RUN — nothing is changed."
  echo
  show_plan
  exit 0
fi

# --- pre-flight (root, read-only) ----------------------------------------------
[[ $EUID -eq 0 ]] || die "run with sudo (or use --dry-run)"
command -v "$UFW" >/dev/null || die "ufw not installed (sudo apt install ufw)"
command -v systemd-run >/dev/null || die "systemd-run not found"
command -v flock >/dev/null || die "flock not found (util-linux)"
[[ -f $HERE/$DROPIN_SRC ]] || die "missing $HERE/$DROPIN_SRC"
systemctl is-enabled --quiet ufw.service \
  || die "ufw.service is not enabled: ufw would not load at boot, and the boot-time rollback runs inside it (sudo systemctl enable ufw.service)"
BOOT_ID=$(cat "$BOOT_ID_FILE" 2>/dev/null || true)
[[ $BOOT_ID =~ ^[0-9a-f-]{36}$ ]] || die "cannot read the boot id from $BOOT_ID_FILE"
grep -qs '^IPV6=yes' /etc/default/ufw || die "/etc/default/ufw must have IPV6=yes (the IPv6 rules would be skipped)"
[[ -n $(ss -ltnH "sport = :$SSH_PORT") ]] || die "sshd is not listening on :$SSH_PORT — refusing (lockout risk)"
( exec </dev/tty ) 2>/dev/null || die "needs an interactive terminal for the confirmation step"
# Root-only (0600) lock files: anyone who could open one could hold its lock and stall
# a rollback. (/run is root-only, so nobody else can create them first.)
UMASK0=$(umask)
umask 077
exec 9>/run/ai-fw.lock 8>"$RB_LOCK"
umask "$UMASK0"
flock -n 9 || die "another setup-firewall.sh run is in progress"
# The rollback lock is held from here until the confirmation prompt, so no rollback.sh
# (not even a manual one) runs while this run snapshots and applies.
flock -n 8 || die "a rollback ($SNAP/rollback.sh) is running — let it finish, then re-run"
# A run interrupted between the two renames of its snapshot swap (it had changed
# nothing yet) leaves the previous snapshot as $SNAP.old: put it back.
if [[ ! -e $SNAP && -d $SNAP.old ]]; then
  mv "$SNAP.old" "$SNAP"
  echo "note: put back $SNAP from an interrupted run"
fi
# An unfinished earlier run still owns the snapshot: re-snapshotting now would make
# its unconfirmed, half-applied state the rollback target.
case $(timer_state) in
  waiting|running) die "a previous run's dead-man switch ($UNIT.timer) is still pending — let it finish (systemctl list-timers $UNIT.timer), then re-run" ;;
esac
case $(systemctl show -p ActiveState --value "$UNIT.service" 2>/dev/null || true) in
  active|activating|deactivating|reloading)   # activating includes waiting to retry
    die "a previous run's rollback ($UNIT.service) is running or retrying — let it finish, then re-run" ;;
esac
[[ -e $SNAP/PENDING ]] && die "an earlier run was never confirmed and its rollback did not complete ($SNAP/PENDING exists).
Restore the state from before that run first:  sudo /bin/sh $SNAP/rollback.sh   (then re-run this script)"

# Live state that rollback.sh restores and verifies besides the files: whether ufw is
# enabled and its rules loaded (per family; IPV6=yes makes IPv6 mandatory), the
# built-in chains' policies (ufw's stop sets them all to ACCEPT, which would open
# Docker's FORWARD DROP) and DOCKER-USER's rules. Each family's filter table is read in
# one go, so a failed read is never mistaken for "absent". ufw's state in a table is
# "loaded" while its user chain exists (its start is a no-op then), "unloaded" once that
# is gone and none of its chains holds a rule (its stop leaves its main chains behind,
# empty and still jumped to from the built-in chains: harmless), else "leftover" (its
# stop ignores flush errors).
FAMS="iptables ip6tables"
ufw_state() {
  if grep -qx -- "-N $2" <<<"$1"; then echo loaded
  elif grep -q '^-A ufw' <<<"$1"; then echo leftover
  else echo unloaded; fi
}
capture_live() {
  local x tbl uchain want got ch pol
  WAS_ENABLED=no
  if grep -qs '^ENABLED=yes' /etc/ufw/ufw.conf; then WAS_ENABLED=yes; fi
  POLICIES="" DU_STATE=absent DU_RULES=""
  for x in $FAMS; do
    tbl=$("$x" -w -S 2>/dev/null) || die "cannot read the $x filter table ($x -S)"
    uchain=ufw-user-input
    [[ $x == ip6tables ]] && uchain=ufw6-user-input
    want=unloaded
    [[ $WAS_ENABLED == yes ]] && want=loaded
    got=$(ufw_state "$tbl" "$uchain")
    [[ $got == "$want" ]] || die "ufw is in an inconsistent state: /etc/ufw/ufw.conf has ENABLED=$WAS_ENABLED but ufw's $x rules are $got (expected $want).
Settle it first (sudo ufw disable, then sudo ufw enable if it should be on), then re-run."
    for ch in INPUT OUTPUT FORWARD; do
      pol=$(awk -v c="$ch" '$1 == "-P" && $2 == c {print $3; exit}' <<<"$tbl")
      [[ $pol =~ ^(ACCEPT|DROP)$ ]] || die "cannot read the $x $ch policy"
      POLICIES+="${POLICIES:+ }$x:$ch:$pol"
    done
    if [[ $x == iptables ]] && grep -qx -- '-N DOCKER-USER' <<<"$tbl"; then
      DU_STATE=present
      DU_RULES=$(grep '^-A DOCKER-USER ' <<<"$tbl" || true)
    fi
  done
}
live_state() { printf '%s|%s|%s|%s' "$WAS_ENABLED" "$POLICIES" "$DU_STATE" "$DU_RULES"; }
capture_live
if [[ $DU_STATE == absent ]]; then
  if docker info >/dev/null 2>&1; then
    die "Docker is running but its DOCKER-USER chain isn't visible via $(iptables --version) — iptables backend mismatch?"
  fi
  echo "note: Docker isn't running; the DOCKER-USER block will apply when ufw loads."
fi
# The managed block owns DOCKER-USER (ufw re-declares = flushes it on every load), so
# refuse if someone else put rules there.
foreign=$(grep -v -e 'ai-fw' -e '^-A DOCKER-USER -j RETURN$' <<<"$DU_RULES" || true)
[[ -z $foreign ]] || die "DOCKER-USER has rules not managed by this script:
$foreign
Move them into $AFTER (inside the managed block) or remove them first."

for r in "${RULES[@]}"; do
  # shellcheck disable=SC2086  # specs are plain space-separated ufw arguments
  "$UFW" --dry-run ${r%%|*} comment "$TAG:$HASH ${r#*|}" >/dev/null \
    || die "ufw rejected rule: ${r%%|*}"
done
# shellcheck disable=SC2086
"$UFW" --dry-run $DOCKER_ALLOW comment "$DOCKER_ALLOW_TAG" >/dev/null || die "ufw rejected rule: $DOCKER_ALLOW"
# shellcheck disable=SC2086
"$UFW" --dry-run $DOCKER_REJECT comment "$DOCKER_REJECT_TAG" >/dev/null || die "ufw rejected rule: $DOCKER_REJECT"
for t in "${TRANSIENTS[@]}"; do
  # shellcheck disable=SC2086
  "$UFW" --dry-run $t comment "$TRANSIENT_TAG" >/dev/null || die "ufw rejected rule: $t"
done

# Everything containers need must already work AND stay inside the allowlist —
# e.g. an Open WebUI tool server configured with a host address instead of
# http://mcpo:8000/<name> would silently break once the reject rule is live.
# Pre-flight checks only the ports the live rule already allows: a port being ADDED in
# this run cannot connect yet (it is verified after apply). First run: use the new list.
LIVE_PORTS=$("$UFW" status 2>/dev/null | sed -n "s|^.*[[:space:]]\([0-9][0-9,]*\)/tcp[[:space:]]*ALLOW IN[[:space:]]*$DOCKER_NET[[:space:]].*ai-fw:docker-.*|\1|p" | head -1)
PRE_PORTS=${LIVE_PORTS:-$DOCKER_HOST_PORTS}
echo "== Pre-flight: what must keep working (verify-firewall.sh --preflight, ports $PRE_PORTS)"
DOCKER_HOST_PORTS=$PRE_PORTS "$HERE/verify-firewall.sh" --preflight | sed 's/^/  /' \
  || die "pre-flight FAILs above would break (or already are broken) — fix them, then re-run"
echo

ARMED=0 KEPT=0 ROLLED=0 TMP_AFTER="" STAGE=""
rollback_now() {
  (( ROLLED )) && return 0
  ROLLED=1
  trap '' INT TERM HUP PIPE   # let the restore finish even if the session dies
  exec 8>&-                   # drop our hold on the rollback lock; rollback.sh takes it
  echo ">> Rolling back to the pre-run firewall state..." >&2 || true
  if /bin/sh "$SNAP/rollback.sh"; then
    systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
    echo ">> Rolled back and verified: the firewall is as it was before this run." >&2 || true
  else
    echo ">> ROLLBACK FAILED (see above; journalctl -t ai-fw). If still armed, the dead-man switch" >&2 || true
    echo ">> runs at its deadline (retrying on failure); the ufw.service drop-in restores the files" >&2 || true
    echo ">> at the next boot. Retry now: sudo /bin/sh $SNAP/rollback.sh" >&2 || true
  fi
}
on_exit() {
  local rc=$?
  trap - EXIT
  set +e   # best effort from here: nothing may stop the rollback below
  if (( ARMED && !KEPT )); then
    (( rc )) || rc=1
    echo "ERROR/abort (exit $rc) before confirmation." >&2 || true
    rollback_now
  fi
  [[ -n $TMP_AFTER ]] && rm -f "$TMP_AFTER"
  # Interrupted between the swap's two renames: the previous snapshot is in $SNAP.old.
  [[ ! -e $SNAP && -d $SNAP.old ]] && mv "$SNAP.old" "$SNAP"
  [[ -n $STAGE ]] && rm -rf "$STAGE"
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# SSH dropped: the terminal is gone, so stop writing to it before rolling back.
trap 'exec </dev/null >/dev/null 2>&1; exit 129' HUP

# The config the plan is built from. The confirmation prompt can wait indefinitely, so
# the snapshot taken after it must still match this (and the live state its capture).
CONF_FILES=(/etc/ufw/user.rules /etc/ufw/user6.rules "$AFTER" /etc/ufw/ufw.conf /etc/default/ufw)
fingerprint() { local f; for f in "$@"; do sha256sum <"$f" || return 1; done; }
FP=$(fingerprint "${CONF_FILES[@]}") || die "cannot read the ufw config"
LIVE0=$(live_state)

TMP_AFTER=$(mktemp)
# Drop any previous managed block plus the blank lines before it / at EOF, so
# re-runs are byte-identical.
awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
  $0 == b {skip = 1; buf = ""; next}
  skip {if ($0 == e) skip = 0; next}
  /^[[:space:]]*$/ {buf = buf $0 ORS; next}
  {printf "%s", buf; buf = ""; print}
  END {if (skip) exit 3}' "$AFTER" >"$TMP_AFTER" \
  || die "$AFTER has a '$BEGIN_MARK' line without its '$END_MARK' — fix that by hand, then re-run"
printf '\n%s\n' "$DOCKER_USER_BLOCK" >>"$TMP_AFTER"
iptables-restore --test --noflush < <(sed -n "/^\\*filter/,/^COMMIT/p" <<<"$DOCKER_USER_BLOCK") \
  || die "iptables-restore rejected the DOCKER-USER block"

echo "== Current state"
"$UFW" status verbose | sed 's/^/  /'
echo "  chain policies: $POLICIES"
echo "  DOCKER-USER: $(grep -c '^-A ' <<<"$DU_RULES" || true) rule(s)"
echo
echo "== Plan"
show_plan | sed 's/^/  /'
echo
read -r -p "Apply this firewall? [y/N] " ans </dev/tty
[[ $ans =~ ^[Yy]$ ]] || { echo "Aborted; nothing changed."; exit 0; }

# --- boot-time safety net, snapshot, dead-man switch ---------------------------
# The drop-in does nothing unless $SNAP/PENDING exists.
dropin_loaded() { [[ " $(systemctl show -p DropInPaths --value ufw.service 2>/dev/null) " == *" $DROPIN "* ]]; }
if ! cmp -s "$HERE/$DROPIN_SRC" "$DROPIN"; then
  mkdir -p "${DROPIN%/*}"
  install -m 0644 -o root -g root "$HERE/$DROPIN_SRC" "$DROPIN"
  systemctl daemon-reload
fi
if ! dropin_loaded; then
  systemctl daemon-reload
  dropin_loaded || die "systemd does not list $DROPIN for ufw.service (systemctl show -p DropInPaths ufw.service)"
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
# Built aside and swapped in whole: until then $SNAP stays the previous run's complete
# snapshot (the "undo a kept run" target), never a mix of two runs.
STAGE=$SNAP.new
rm -rf "$STAGE"
install -d -m 0700 "$STAGE"
cp -p /etc/ufw/user.rules /etc/ufw/user6.rules "$AFTER" /etc/ufw/ufw.conf "$STAGE"/
cp -p /etc/default/ufw "$STAGE/default-ufw"
for f in user.rules user6.rules after.rules ufw.conf; do
  cmp -s "/etc/ufw/$f" "$STAGE/$f" || die "snapshot of /etc/ufw/$f does not match"
done
cmp -s /etc/default/ufw "$STAGE/default-ufw" || die "snapshot of /etc/default/ufw does not match"
[[ $(fingerprint "$STAGE"/{user.rules,user6.rules,after.rules,ufw.conf,default-ufw}) == "$FP" ]] \
  || die "the ufw config changed while waiting for your answer — nothing was changed; re-run"
capture_live
[[ $(live_state) == "$LIVE0" ]] \
  || die "the live firewall changed while waiting for your answer — nothing was changed; re-run"
printf '%s\n' "$DU_RULES" | sed '/^$/d' >"$STAGE/docker-user.rules"
# rollback.sh = this run's settings + a fixed body. The body is a quoted heredoc
# (nothing in it expands here) and only uses the paths set in the header.
{
  cat <<EOF
#!/bin/sh
# Generated by setup-firewall.sh at $STAMP: restore the firewall exactly as it was
# before that run.
#   sh rollback.sh            files + live state (setup's error path; undo a kept run)
#   sh rollback.sh --deadman  the same, but only while the run is unconfirmed (PENDING)
#   sh rollback.sh --boot     files only, from the ufw.service drop-in before ufw loads,
#                             and only for an unconfirmed run from an earlier boot
# Exits 0 only once the restore is verified; only then is the PENDING marker cleared.
S='$SNAP'
LOCK='$RB_LOCK'
ETC='/etc/ufw'
DEF='/etc/default/ufw'
UFW='$UFW'
BOOT_ID_FILE='$BOOT_ID_FILE'
STAMP='$STAMP'
WAS_ENABLED='$WAS_ENABLED'
FAMS='$FAMS'
POLICIES='$POLICIES'
EOF
  cat <<'EOF'
MODE=${1:-live}
say() { echo "ai-fw rollback: $*"; logger -t ai-fw "$*" 2>/dev/null || true; }
case $MODE in
  live|--deadman|--boot) ;;
  *) echo "usage: sh $0 [--deadman|--boot]" >&2; exit 2 ;;
esac
if [ "$MODE" = --boot ]; then
  [ -e "$S/PENDING" ] || exit 0
  # ufw.service restarted during a run in this same boot: that run's dead-man switch
  # (or its error path) owns the rollback.
  if grep -qx "boot_id=$(cat "$BOOT_ID_FILE" 2>/dev/null)" "$S/PENDING"; then
    say "boot: the unconfirmed run is from this boot; its dead-man switch handles it"
    exit 0
  fi
fi
# Root-only lock file (0600): anyone who could open it could hold the lock and stall
# every rollback.
um=$(umask); umask 077
exec 8>"$LOCK" && flock 8 || { say "cannot lock $LOCK"; exit 1; }
umask "$um"
# A later run that got the lock first has replaced $S, this script included: its
# files must not be restored with this run's settings.
if ! grep -qx "STAMP='$STAMP'" "$S/rollback.sh" 2>/dev/null; then
  say "$S now holds a later run's snapshot, not the $STAMP one; nothing done"
  exit 1
fi
# "keep" clears PENDING under this lock, so a dead-man run that was already waiting
# for it does nothing.
case $MODE in
  --deadman|--boot)
    [ -e "$S/PENDING" ] || { say "nothing to roll back (confirmed or already rolled back)"; exit 0; } ;;
esac
fail=0
bad() { say "FAILED: $*"; fail=1; }
uchain() { if [ "$1" = ip6tables ]; then echo ufw6-user-input; else echo ufw-user-input; fi; }
# Each check reads a family's whole filter table once; a failed read is a failure,
# never "absent".
tbl() { $1 -w -S 2>/dev/null; }
has_chain() { printf '%s\n' "$1" | grep -qx -- "-N $2"; }
# loaded: ufw's user chain exists (its start is a no-op then); unloaded: that is gone
# and none of its chains holds a rule (the empty chains its stop leaves behind, and the
# jumps to them, are harmless); leftover: neither (its stop ignores flush errors).
ufw_state() {
  if has_chain "$1" "$2"; then echo loaded
  elif printf '%s\n' "$1" | grep -q '^-A ufw'; then echo leftover
  else echo unloaded; fi
}
say "restoring the firewall from before the $STAMP run ($MODE)"
if [ "$MODE" != --boot ]; then
  [ -f "$S/docker-user.rules" ] || bad "missing $S/docker-user.rules"
  # Unload first: ufw's start does nothing while its user chain exists, so only an
  # unloaded ufw makes the enable below load the restored files. A failing disable is
  # only a warning: everything it affects (chains, policies, ufw.conf) is checked below.
  "$UFW" --force disable >/dev/null 2>&1 || say "warning: ufw disable failed (the checks below decide)"
  for x in $FAMS; do
    if ! t=$(tbl $x); then bad "cannot read the $x filter table"
    elif has_chain "$t" "$(uchain $x)"; then bad "ufw's $x rules are still loaded after ufw disable"; fi
  done
fi
for f in user.rules user6.rules after.rules ufw.conf; do
  { cp -p "$S/$f" "$ETC/$f" && cmp -s "$S/$f" "$ETC/$f"; } || bad "restore $ETC/$f"
done
{ cp -p "$S/default-ufw" "$DEF" && cmp -s "$S/default-ufw" "$DEF"; } || bad "restore $DEF"
sync || bad "sync the restored files to disk"
if [ "$MODE" != --boot ]; then
  if [ "$WAS_ENABLED" = yes ]; then
    "$UFW" --force enable >/dev/null 2>&1 || bad "ufw enable"
  fi
  for p in $POLICIES; do
    x=${p%%:*}; rest=${p#*:}; ch=${rest%%:*}; want=${rest#*:}
    $x -w -P "$ch" "$want" || bad "$x -P $ch $want"
  done
  # ufw's stop leaves DOCKER-USER (and the applied rules in it) alone, so put back the
  # pre-run rules here. Restoring a declared chain flushes it on both iptables backends
  # (and creates it if it is gone), but an empty snapshot restores nothing, hence the
  # explicit flush. A chain that ufw created stays (empty).
  if ! t=$(tbl iptables); then
    bad "cannot read the iptables filter table"
  else
    if has_chain "$t" DOCKER-USER; then
      iptables -w -F DOCKER-USER || bad "iptables -F DOCKER-USER"
    fi
    if [ -s "$S/docker-user.rules" ]; then
      { echo '*filter'; echo ':DOCKER-USER - [0:0]'; cat "$S/docker-user.rules"; echo COMMIT; } \
        | iptables-restore -w --noflush || bad "restore the DOCKER-USER rules"
    fi
  fi
  # Verify the live state, not just the exit codes.
  for x in $FAMS; do
    if ! t=$(tbl $x); then bad "cannot read the $x filter table"; continue; fi
    want=unloaded
    if [ "$WAS_ENABLED" = yes ]; then want=loaded; fi
    got=$(ufw_state "$t" "$(uchain $x)")
    [ "$got" = "$want" ] || bad "ufw's $x rules are $got, expected $want"
    for p in $POLICIES; do
      case $p in "$x":*) ;; *) continue ;; esac
      rest=${p#*:}; ch=${rest%%:*}; want=${rest#*:}
      got=$(printf '%s\n' "$t" | awk -v c="$ch" '$1 == "-P" && $2 == c {print $3; exit}')
      [ "$got" = "$want" ] || bad "$x $ch policy is ${got:-unreadable}, expected $want"
    done
    # A missing chain reads as no rules: right only if the snapshot had none either.
    if [ "$x" = iptables ]; then
      [ "$(printf '%s\n' "$t" | grep '^-A DOCKER-USER ')" = "$(cat "$S/docker-user.rules" 2>/dev/null)" ] \
        || bad "DOCKER-USER differs from $S/docker-user.rules"
    fi
  done
fi
if [ $fail -ne 0 ]; then
  say "ROLLBACK FAILED - the snapshot stays in $S; retry: sudo /bin/sh $S/rollback.sh"
  exit 1
fi
if ! rm -f "$S/PENDING" || [ -e "$S/PENDING" ]; then
  say "FAILED to remove $S/PENDING"; exit 1
fi
# Best effort: should the removal be lost to a power cut, the boot drop-in only
# restores the same files again.
sync || say "warning: sync after removing $S/PENDING failed"
say "firewall rollback complete (verified)"
EOF
} >"$STAGE/rollback.sh"
chmod 0700 "$STAGE/rollback.sh"
sh -n "$STAGE/rollback.sh" || die "generated rollback.sh does not parse"
# Swap the staged snapshot in. A crash here leaves the old snapshot (in $SNAP, or in
# $SNAP.old for the next run to put back), never a half-built one, and nothing has
# been changed yet.
rm -rf "$SNAP.old"
if [[ -e $SNAP ]]; then mv "$SNAP" "$SNAP.old"; fi
mv "$STAGE" "$SNAP"
STAGE=""
rm -rf "$SNAP.old"

systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
systemctl reset-failed "$UNIT.service" "$UNIT.timer" >/dev/null 2>&1 || true
# The rollback lock (held since pre-flight) stays held while applying + verifying: a
# dead-man switch that fires meanwhile waits for it instead of restoring underneath a
# half-applied change.
ARMED=1   # from here on, any exit other than a confirmed "keep" rolls back
printf 'stamp=%s\nboot_id=%s\n' "$STAMP" "$BOOT_ID" >"$SNAP/PENDING"
grep -qx "boot_id=$BOOT_ID" "$SNAP/PENDING" || die "could not write $SNAP/PENDING"
sync   # the snapshot and PENDING are on disk before anything changes
systemd-run --quiet --unit="$UNIT" --on-active="${CONFIRM_SECS}s" --timer-property=AccuracySec=1s \
  -p Restart=on-failure -p RestartSec=30s -p StartLimitIntervalSec=1h -p StartLimitBurst=10 \
  --description="ai-fw: roll back the firewall unless confirmed (ADR-0028)" /bin/sh "$SNAP/rollback.sh" --deadman
ARM_TS=$(date +%s)
echo "Dead-man switch armed: rollback in ${CONFIRM_SECS}s unless confirmed (or at boot if the box restarts first)."

# --- apply ---------------------------------------------------------------------
echo "== Applying"
install -m 0640 -o root -g root "$TMP_AFTER" "$AFTER"

for r in "${RULES[@]}"; do
  # shellcheck disable=SC2086
  "$UFW" ${r%%|*} comment "$TAG:$HASH ${r#*|}" | sed 's/^/  /'
done

ADDED=()
read_added() {  # the "ufw ..." lines of `ufw show added` -> ADDED; dies if ufw fails
  local out
  out=$("$UFW" show added) || die "'ufw show added' failed"
  mapfile -t ADDED < <(grep '^ufw ' <<<"$out" || true)
}
del_rule_line() {  # delete the rule that a `ufw show added` line describes
  local spec
  spec=$(sed -E "s/^ufw //; s/ comment '[^']*'\$//" <<<"$1")
  # shellcheck disable=SC2086
  "$UFW" --force delete $spec | sed 's/^/    /'
}
docker_pair_on_top() {
  (( ${#ADDED[@]} >= 2 )) \
    && [[ ${ADDED[0]} == *"comment '$DOCKER_ALLOW_TAG'" && ${ADDED[1]} == *"comment '$DOCKER_REJECT_TAG'" ]]
}

# Container rules: (re)insert at the top whenever they aren't the current pair there.
read_added
old_docker=()
for line in "${ADDED[@]}"; do
  if [[ $line == *"comment '$TAG:docker-"* ]]; then old_docker+=("$line"); fi
done
if (( ${#old_docker[@]} == 2 )) && docker_pair_on_top; then
  echo "  container rules current ($TAG:docker-$DHASH)"
else
  # Fail closed while the pair moves: the transient rejects go on top FIRST, so no
  # container connection can fall through to a broader allow (SSH from anywhere) in
  # between. Allowlisted ports are refused for those few seconds too.
  for t in "${TRANSIENTS[@]}"; do
    # shellcheck disable=SC2086
    "$UFW" --force delete $t >/dev/null   # leftovers of an interrupted run
  done
  for t in "${TRANSIENTS[@]}"; do
    # shellcheck disable=SC2086
    "$UFW" insert 1 $t comment "$TRANSIENT_TAG" | sed 's/^/  /'
  done
  for line in "${old_docker[@]}"; do del_rule_line "$line"; done
  # shellcheck disable=SC2086
  "$UFW" insert 1 $DOCKER_REJECT comment "$DOCKER_REJECT_TAG" | sed 's/^/  /'
  # shellcheck disable=SC2086
  "$UFW" insert 1 $DOCKER_ALLOW comment "$DOCKER_ALLOW_TAG" | sed 's/^/  /'
  for t in "${TRANSIENTS[@]}"; do
    # shellcheck disable=SC2086
    "$UFW" --force delete $t | sed 's/^/  /'
  done
fi

# Prune managed rules from earlier runs (other tags, incl. transient leftovers).
read_added
for line in "${ADDED[@]}"; do
  if [[ $line == *"comment '$TAG:"* && $line != *"comment '$TAG:$HASH "* \
        && $line != *"comment '$TAG:docker-$DHASH "* ]]; then
    echo "  pruning stale: $line"
    del_rule_line "$line"
  fi
done
read_added
docker_pair_on_top || die "the container rules are not the first two ufw rules (ufw show added)"

"$UFW" default deny incoming | sed 's/^/  /'
"$UFW" default allow outgoing | sed 's/^/  /'
if [[ $WAS_ENABLED == yes ]]; then
  "$UFW" reload | sed 's/^/  /'
else
  "$UFW" --force enable | sed 's/^/  /'
fi

# --- check ---------------------------------------------------------------------
echo
echo "== Firewall now"
"$UFW" status verbose | sed 's/^/  /'
echo "  DOCKER-USER:"
iptables -S DOCKER-USER 2>&1 | sed 's/^/    /' || true

echo
echo "== Verify (strict: any FAIL rolls back)"
vrc=0
AI_FW_SETUP=1 DOCKER_HOST_PORTS=$DOCKER_HOST_PORTS DOCKER_NET=$DOCKER_NET LAN_IF=$LAN_IF LAN_CIDR=$LAN_CIDR \
  "$HERE/verify-firewall.sh" | sed 's/^/  /' || vrc=$?
if (( vrc != 0 )); then
  echo ">>> verify-firewall.sh did not pass (exit $vrc) — rolling back; nothing to confirm." >&2
  exit 1
fi
exec 8>&-   # release the rollback lock: from here on the dead-man switch may fire

lan_ip=$(ip -4 -o addr show dev "$LAN_IF" | awk '{sub(/\/.*/, "", $4); print $4; exit}')
left=$(( ARM_TS + CONFIRM_SECS - 15 - $(date +%s) ))
(( left >= 30 )) || { echo "Too little time left to confirm (${left}s); re-run with a larger CONFIRM_SECS." >&2; exit 1; }
cat <<EOF

== Confirm within ${left}s
The firewall is ON. From your Mac, WITHOUT closing this session:
  1. open a NEW SSH session:   ssh ${SUDO_USER:-brad}@${lan_ip:-<lan-ip>}   (and/or via Tailscale)
  2. load Open WebUI (http://${lan_ip:-<lan-ip>}:3000) and the dashboard (:9095)
Anything else, Ctrl-C, a dropped session or no answer = roll back.
EOF
ans=""
read -r -t "$left" -p "Type 'keep' to keep the firewall: " ans </dev/tty || true
if [[ $ans != keep ]]; then
  echo
  echo "Not confirmed."
  exit 1   # on_exit rolls back
fi
# Serialize with the dead-man switch: under the lock, a timer that is still waiting
# means no rollback has run. (If it fires between this check and the stop, its
# rollback.sh --deadman waits for the lock, finds PENDING gone and does nothing.)
exec 8>"$RB_LOCK"
flock 8
state=$(timer_state)
if [[ $state != waiting ]] || ! systemctl stop "$UNIT.timer"; then
  echo "Too late: the dead-man switch already fired (timer: ${state:-unknown}); re-checking its rollback." >&2
  exit 1   # on_exit re-runs the idempotent rollback and reports the verified result
fi
if ! rm -f "$SNAP/PENDING" || [[ -e $SNAP/PENDING ]]; then
  echo "Could not clear $SNAP/PENDING — rolling back rather than leave a boot-time rollback armed." >&2
  exit 1
fi
if ! sync; then   # a crash right after "keep" must not resurrect PENDING (= a rollback at boot)
  echo "warning: sync failed — if the box loses power in the next few seconds, the boot-time safety net may still restore the state from before this run." >&2
fi
KEPT=1
exec 8>&-
echo "Kept. Dead-man switch disarmed; ufw stays enabled across reboots."
echo "Undo later: sudo /bin/sh $SNAP/rollback.sh (restores the state from before this run)."

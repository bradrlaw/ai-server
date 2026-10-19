#!/usr/bin/env bash
# Verify the host firewall (ufw + DOCKER-USER, ADR-0028). Read-only: changes nothing,
# never prints secrets. Runs as brad (needs the docker group) or root (adds the
# rule-level checks: ufw state + rule order, the managed DOCKER-USER block).
#
# Proves, from inside the containers, that:
#   - every ALLOWLISTED container->host port still connects (LiteLLM, ComfyUI open,
#     MCP bridges, status JSON, llama-swap-mode MCP) — i.e. the stack isn't broken;
#   - every OTHER host port that has a listener is REFUSED fast (SSH, Samba,
#     plan-build, AI Toolkit, the login-gated ComfyUIs) — i.e. a prompt-injected
#     agent container can't reach them;
#   - a container on another Docker network can't reach a published port through
#     the host's address (that path is forwarded, not seen by the ufw rules);
#   - Open WebUI's admin tool servers stay reachable; container DNS + egress work.
# Off-host checks (LAN / Tailscale / non-LAN sources) need another machine; the
# commands are printed at the end.
#
# Usage: scripts/verify-firewall.sh             full check (sudo for the rule-level part)
#        scripts/verify-firewall.sh --preflight
#            only what must ALREADY pass before the firewall goes on (allowlisted
#            ports, Open WebUI tool servers, DNS/egress); setup-firewall.sh runs it
#            and refuses to continue on a FAIL.
# Exit:  0 = no FAIL (SKIPs allowed)   1 = FAIL   2 = usage error
#        3 = inconclusive: full mode without root (rule-level checks not run)
# Env:   DOCKER_HOST_PORTS DOCKER_NET LAN_IF LAN_CIDR (defaults: as setup-firewall.sh)
#        AI_FW_SETUP=1  set by setup-firewall.sh while its own run is unconfirmed
set -uo pipefail

PRE=0
case "${1:-}" in
  -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; exit 0 ;;
  --preflight) PRE=1 ;;
  "") ;;
  *) echo "unknown arg: $1 (see --help)"; exit 2 ;;
esac

DOCKER_HOST_PORTS=${DOCKER_HOST_PORTS:-4000,8188,9000,9001,9095,9120}
DOCKER_NET=${DOCKER_NET:-172.16.0.0/12}
AI_FW_SETUP=${AI_FW_SETUP:-0}
TAG=ai-fw
SNAP=/etc/ufw/ai-fw-snapshot
BEGIN_MARK="# BEGIN ai-fw DOCKER-USER (managed by /srv/ai/scripts/setup-firewall.sh, ADR-0028)"
HOST_GW=host.docker.internal
SECURE_GW=172.31.250.1     # host side of the internal mcp-secure network (ADR-0026)
T_OK=5                     # probes that SHOULD connect
T_BLOCK=3                  # probes that must be refused (a REJECT answers instantly)

PASS=0 FAIL=0 SKIP=0 INCONCLUSIVE=0
ok()   { echo "  PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
hdr()  { echo; echo "== $*"; }

LAN_IF=${LAN_IF:-$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i+1); exit}}')}
LAN_CIDR=${LAN_CIDR:-$(ip -4 route show dev "${LAN_IF:-none}" proto kernel scope link 2>/dev/null | awk 'NR == 1 {print $1}')}
LAN_IP=$(ip -4 -o addr show dev "${LAN_IF:-none}" 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')
TS_IP=$(ip -4 -o addr show dev tailscale0 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')
# Every host address a container could dial (LAN, tailnet, Docker gateways). Not
# loopback: 127.0.0.1 inside a container is the container itself.
HOST_IPS=$(ip -4 -o addr show 2>/dev/null | awk '$2 != "lo" {sub(/\/.*/, "", $4); print $4}' | paste -sd' ' -)

# TCP connect probe run INSIDE a container. Prints "<target> <open|refused|timeout|...> <ms>".
PROBE='
import socket, sys, time
t = float(sys.argv[1])
for tgt in sys.argv[2:]:
    host, port = tgt.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(t)
    t0 = time.monotonic()
    try:
        s.connect((host, int(port)))
        r = "open"
    except socket.timeout:
        r = "timeout"
    except ConnectionRefusedError:
        r = "refused"
    except socket.gaierror:
        r = "dns-fail"
    except OSError as e:
        r = "err%s" % e.errno
    finally:
        s.close()
    print(tgt, r, int((time.monotonic() - t0) * 1000))
'
cprobe() { # container timeout target... -> lines "<target> <result> <ms>"
  local c=$1 t=$2; shift 2
  docker exec "$c" python3 -c "$PROBE" "$t" "$@" 2>/dev/null || true
}
running() { [[ $(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null) == true ]]; }
listening() { [[ -n $(ss -ltnH "sport = :$1" 2>/dev/null) ]]; }
nets() { docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$1" 2>/dev/null; }
share_net() {  # do containers $1 and $2 share a Docker network?
  local a b
  for a in $(nets "$1"); do for b in $(nets "$2"); do [[ $a == "$b" ]] && return 0; done; done
  return 1
}

# --- 1. firewall state --------------------------------------------------------
if (( !PRE )); then
hdr "firewall state"
if grep -qs '^ENABLED=yes' /etc/ufw/ufw.conf; then
  ok "ufw enabled at boot (/etc/ufw/ufw.conf ENABLED=yes)"
else
  bad "ufw is NOT enabled — run: sudo scripts/setup-firewall.sh"
fi
if [[ $EUID -ne 0 ]]; then
  skip "rule-level checks (ufw status + rule order, DOCKER-USER) need root: sudo $0"
  INCONCLUSIVE=1
else
  st=$(ufw status verbose 2>&1) || st=""
  if grep -q '^Status: active' <<<"$st"; then ok "ufw status: active"; else bad "ufw status: not active"; fi
  if grep -q 'deny (incoming)' <<<"$st"; then ok "default incoming policy: deny"; else bad "default incoming policy is not deny"; fi

  # Same recipe as setup-firewall.sh (keep in sync): the tag encodes the port list.
  DOCKER_ALLOW="allow proto tcp from $DOCKER_NET to any port $DOCKER_HOST_PORTS"
  DOCKER_REJECT="reject from $DOCKER_NET"
  DHASH=$(printf '%s\n%s\n' "$DOCKER_ALLOW" "$DOCKER_REJECT" | sha256sum | cut -c1-8)
  A=()
  if added=$(ufw show added 2>&1); then
    mapfile -t A < <(grep '^ufw ' <<<"$added" || true)
  else
    bad "'ufw show added' failed"
  fi
  if [[ ${A[0]:-} == *"comment '$TAG:docker-$DHASH allowlist'" \
        && ${A[1]:-} == *"comment '$TAG:docker-$DHASH reject other ports'" ]]; then
    ok "container rules are the first two ufw rules (allow tcp $DOCKER_HOST_PORTS, then reject; $TAG:docker-$DHASH)"
  else
    bad "the first two ufw rules aren't the container allowlist + reject for tcp $DOCKER_HOST_PORTS — re-run setup-firewall.sh"
  fi
  ssh_ok=0 transient=0
  for l in "${A[@]}"; do
    [[ $l == "ufw allow 22/tcp comment '$TAG:"* ]] && ssh_ok=1
    [[ $l == *"comment '$TAG:transient"* ]] && transient=1
  done
  if (( ssh_ok )); then ok "SSH 22/tcp allowed (anti-lockout rule)"; else bad "managed SSH allow rule (22/tcp) missing"; fi
  if (( transient )); then bad "transient container-lockout rules left over (interrupted setup run?) — re-run setup-firewall.sh"; fi

  if grep -qsxF "$BEGIN_MARK" /etc/ufw/after.rules; then
    ok "managed DOCKER-USER block present in /etc/ufw/after.rules (persists across reboots)"
  else
    bad "managed DOCKER-USER block missing from /etc/ufw/after.rules"
  fi
  # The block's 6 rules, in order (substring match: iptables canonicalises the text).
  EXPECT=(
    "-i $LAN_IF |! -s $LAN_CIDR |--ctstate NEW|-j LOG|log non-LAN to published ports (LAN interface)"
    "-i $LAN_IF |! -s $LAN_CIDR |--ctstate NEW|-j DROP|drop non-LAN to published ports"
    "-s $DOCKER_NET |--ctstate DNAT|--ctdir ORIGINAL|-j LOG|log containers to published ports"
    "-s $DOCKER_NET |-p tcp |--ctdir ORIGINAL|tcp-reset|reject containers to published ports (tcp)"
    "-s $DOCKER_NET |--ctstate DNAT|--ctdir ORIGINAL|-j REJECT|reject containers to published ports (other)"
    "-j RETURN|-j RETURN|-j RETURN|-j RETURN|return"
  )
  DU=()
  if du=$(iptables -S DOCKER-USER 2>/dev/null); then
    mapfile -t DU < <(grep '^-A DOCKER-USER ' <<<"$du" || true)
    du_ok=1
    if (( ${#DU[@]} != ${#EXPECT[@]} )); then
      du_ok=0
      bad "DOCKER-USER has ${#DU[@]} rules, expected ${#EXPECT[@]} (the managed block only):"
      printf '          %s\n' "${DU[@]}"
    else
      for i in "${!EXPECT[@]}"; do
        IFS='|' read -r s1 s2 s3 s4 what <<<"${EXPECT[$i]}"
        r=${DU[$i]}
        if [[ $r != *"$s1"* || $r != *"$s2"* || $r != *"$s3"* || $r != *"$s4"* ]]; then
          du_ok=0
          bad "DOCKER-USER rule $((i+1)) should $what: $r"
        fi
      done
    fi
    (( du_ok )) && ok "DOCKER-USER: non-LAN -> published ports dropped; containers -> published ports rejected (live)"
  else
    bad "DOCKER-USER chain missing (the managed block isn't loaded)"
  fi
  fwd=$(iptables -S FORWARD 2>/dev/null || true)
  if grep -qxF -- '-A FORWARD -j DOCKER-USER' <<<"$fwd"; then
    ok "FORWARD jumps to DOCKER-USER"
  elif docker info >/dev/null 2>&1; then
    bad "FORWARD has no jump to DOCKER-USER although Docker is running — published ports are unfiltered"
  else
    skip "FORWARD -> DOCKER-USER: Docker isn't running (it adds the jump when it starts)"
  fi

  if [[ -e $SNAP/PENDING ]]; then
    if [[ $AI_FW_SETUP == 1 ]]; then
      skip "unconfirmed change pending (this setup-firewall.sh run)"
    else
      bad "an UNCONFIRMED firewall change is pending ($SNAP/PENDING): the dead-man switch or the next boot rolls it back — restore now with sudo /bin/sh $SNAP/rollback.sh, or re-run setup-firewall.sh"
    fi
  fi
fi
fi

# --- 2. container -> host: the allowlist must still connect --------------------
hdr "container -> host: allowlisted ports must connect ($DOCKER_HOST_PORTS)"
declare -A WHO=(
  [4000]="open-webui|LiteLLM gateway"
  [8188]="open-webui|ComfyUI open (Open WebUI image gen)"
  [9000]="mcpo|ComfyUI MCP bridge (open)"
  [9095]="open-webui|status JSON (chat banner)"
  [9120]="mcpo|llama-swap-mode MCP"
)
for p in ${DOCKER_HOST_PORTS//,/ }; do
  if [[ $p == 9001 ]]; then
    c=mcpo tgt="$SECURE_GW:9001" what="secure ComfyUI MCP bridge via mcp-secure"
  elif [[ -n ${WHO[$p]:-} ]]; then
    c=${WHO[$p]%%|*} tgt="$HOST_GW:$p" what=${WHO[$p]#*|}
  else
    c=open-webui tgt="$HOST_GW:$p" what="custom allowlist entry"
  fi
  if ! listening "$p"; then skip "$c -> $tgt ($what): nothing listening on the host"; continue; fi
  if ! running "$c"; then skip "$c -> $tgt: container not running"; continue; fi
  read -r _ res ms < <(cprobe "$c" "$T_OK" "$tgt")
  if [[ ${res:-} == open ]]; then ok "$c -> $tgt ($what) connects (${ms}ms)"
  else bad "$c -> $tgt ($what): ${res:-no-probe} — allowlist broken?"; fi
done
for c in hermes openclaw; do
  running "$c" || { skip "$c -> $HOST_GW:4000: container not running"; continue; }
  read -r _ res ms < <(cprobe "$c" "$T_OK" "$HOST_GW:4000")
  if [[ ${res:-} == open ]]; then ok "$c -> $HOST_GW:4000 (LiteLLM) connects (${ms}ms)"
  else bad "$c -> $HOST_GW:4000 (LiteLLM): ${res:-no-probe}"; fi
done

# --- 3. container -> host: everything else must be refused ---------------------
if (( !PRE )); then
hdr "container -> host: other listening ports must be refused (agent containers)"
BLOCKED=( "22|$HOST_GW|SSH" "9100|$HOST_GW|plan-build MCP" "8675|$HOST_GW|AI Toolkit"
          "8189|$HOST_GW|ComfyUI secure" "8190|$HOST_GW|ComfyUI qwen" )
[[ -n $LAN_IP ]] && BLOCKED+=( "445|$LAN_IP|Samba (LAN address)"
                              "3000|$LAN_IP|Open WebUI published, same-network hairpin" )
for c in hermes open-webui; do
  running "$c" || { skip "$c: container not running"; continue; }
  for b in "${BLOCKED[@]}"; do
    IFS='|' read -r p h what <<<"$b"
    if [[ ,$DOCKER_HOST_PORTS, == *",$p,"* ]]; then skip "$c -> $h:$p ($what): port is allowlisted"; continue; fi
    if ! listening "$p"; then skip "$c -> $h:$p ($what): nothing listening (can't prove the block)"; continue; fi
    read -r _ res ms < <(cprobe "$c" "$T_BLOCK" "$h:$p")
    case ${res:-} in
      refused) ok "$c -> $h:$p ($what) refused (${ms}ms)" ;;
      open)    bad "$c -> $h:$p ($what) CONNECTS — container->host is not filtered" ;;
      timeout) bad "$c -> $h:$p ($what) dropped, not rejected — reject rule missing/out of order (callers hang)" ;;
      *)       bad "$c -> $h:$p ($what): ${res:-no-probe}" ;;
    esac
  done
done

# From ANOTHER Docker network, a published port dialled via the host's address is
# DNATed + forwarded (same-network hairpin goes through docker-proxy and INPUT), so
# only the DOCKER-USER block can stop it. autoheal (busybox nc, no python) sits on
# docker_default, away from Open WebUI's network; refused vs dropped is told by time.
xc=autoheal xp=3000
if [[ -z $LAN_IP ]]; then skip "cross-network probe: no LAN IPv4 address"
elif ! running "$xc"; then skip "cross-network probe: $xc not running"
elif ! listening "$xp"; then skip "cross-network probe: nothing listening on :$xp"
elif share_net "$xc" open-webui; then skip "cross-network probe: $xc shares a network with open-webui"
else
  t0=$(date +%s%N)
  docker exec "$xc" sh -c "nc -w $T_BLOCK $LAN_IP $xp </dev/null" >/dev/null 2>&1; rc=$?
  ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  case $rc in
    0) bad "$xc -> $LAN_IP:$xp (Open WebUI, published; other Docker network) CONNECTS — the DOCKER-USER container reject is missing" ;;
    1) if (( ms < T_BLOCK * 1000 - 500 )); then
         ok "$xc -> $LAN_IP:$xp (Open WebUI, published; other Docker network) refused (${ms}ms)"
       else
         ok "$xc -> $LAN_IP:$xp (Open WebUI, published; other Docker network) blocked — dropped, not rejected (${ms}ms)"
       fi ;;
    *) bad "$xc -> $LAN_IP:$xp: probe error (docker exec / nc exit $rc)" ;;
  esac
fi
fi

# --- 4. Open WebUI admin tool servers ------------------------------------------
# These are called by the Open WebUI BACKEND (inside the container). They must use
# container DNS (http://mcpo:8000/<name>); a host address (LAN IP, aipcub.local,
# Tailscale IP) goes container->host and is rejected unless its port is allowlisted.
# Fails closed: an unreadable config or an incomplete check is a FAIL. Reads the DB
# read-only; prints only scheme://host[:port] (never paths, keys, userinfo or queries).
hdr "Open WebUI admin tool servers reachable from its backend"
TS_PY='
import json, os, socket, sqlite3, sys, urllib.error, urllib.parse, urllib.request

def clean(x, n=100):
    return "".join(ch for ch in str(x) if ch.isprintable() and ch != "\t")[:n]

def san(url):
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or "?"
        if ":" in host:
            host = "[%s]" % host
        return "%s://%s%s" % (u.scheme, host, ":%d" % u.port if u.port else "")
    except Exception:
        return "<unparseable url>"

def out(*fields):
    print("\t".join(clean(f, 160) for f in fields), flush=True)

def via_host(url):  # -> port string if the URL goes container->host on a non-allowlisted port
    u = urllib.parse.urlsplit(url)
    port = str(u.port or (443 if u.scheme == "https" else 80))
    try:
        v4 = {a[4][0] for a in socket.getaddrinfo(u.hostname, None, socket.AF_INET)}
    except OSError:
        v4 = set()
    return port if (v4 & host_ips and port not in allowed) else None

host_ips, allowed = set(sys.argv[1].split()), set(sys.argv[2].split(","))
try:
    db = sqlite3.connect("file:/app/backend/data/webui.db?mode=ro", uri=True)
    row = db.execute("select value from config where key = ?", ("tool_server.connections",)).fetchone()
    raw = row[0] if row else os.environ.get("TOOL_SERVER_CONNECTIONS", "[]")
    conns = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(conns, str):
        conns = json.loads(conns)
    if not isinstance(conns, list):
        raise TypeError("connections")
except Exception as e:
    out("ERR", "config", "-", type(e).__name__)
    sys.exit(3)

for c in conns:
    name, su = "?", "-"
    try:
        if not isinstance(c, dict):
            out("BAD", name, su, "malformed entry")
            continue
        info = c.get("info") if isinstance(c.get("info"), dict) else {}
        name = info.get("name") or info.get("id") or "?"
        cfg = c.get("config") if isinstance(c.get("config"), dict) else {}
        if cfg.get("enable") is False:
            continue
        url = str(c.get("url") or "").rstrip("/")
        path = str(c.get("path") or "openapi.json")
        # Open WebUI: a path containing "://" is an absolute spec URL.
        spec = path if "://" in path else url + "/" + path.lstrip("/")
        su = san(url or spec)
        if not url and "://" not in path:
            out("BAD", name, su, "no URL")
            continue
        p = next((x for x in (via_host(url) if url else None, via_host(spec)) if x), None)
        if p:
            out("BAD", name, su, "goes via the HOST on :%s, which containers may not reach — use the container name, e.g. http://mcpo:8000/<name>" % p)
            continue
        if str(c.get("type") or "openapi") == "openapi":
            try:
                r = urllib.request.urlopen(spec, timeout=5)
                out("OK", name, su, "HTTP %d" % r.status)
            except urllib.error.HTTPError as e:
                out("OK" if e.code in (401, 403) else "BAD", name, su, "HTTP %d" % e.code)
        else:
            u = urllib.parse.urlsplit(url or spec)
            socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=5).close()
            out("OK", name, su, "tcp connect (type %s)" % c.get("type"))
    except Exception as e:
        out("BAD", name, su, type(e).__name__)
out("END", len(conns))
'
if running open-webui; then
  ts_out=$(docker exec open-webui python3 -c "$TS_PY" "$HOST_IPS" "$DOCKER_HOST_PORTS" 2>/dev/null); ts_rc=$?
  ts_n=0 ts_end=0
  while IFS=$'\t' read -r st name url info; do
    case $st in
      OK)  ok "open-webui -> $name $url ($info)"; ts_n=$((ts_n+1)) ;;
      BAD) bad "open-webui -> $name $url: $info (Admin Panel → Settings → Integrations)"; ts_n=$((ts_n+1)) ;;
      ERR) bad "Open WebUI tool-server config not readable ($info) — can't prove the tool servers keep working" ;;
      END) ts_end=1 ;;
    esac
  done <<<"$ts_out"
  if (( ts_rc == 3 )); then
    :   # ERR reported above
  elif (( ts_rc != 0 || !ts_end )); then
    bad "Open WebUI tool-server check did not complete (exit $ts_rc) — can't prove the tool servers keep working"
  elif (( ts_n == 0 )); then
    skip "no enabled admin tool servers configured"
  fi
else
  skip "open-webui not running"
fi

# --- 5. container DNS + egress -------------------------------------------------
hdr "container DNS + internet egress"
if running open-webui; then
  read -r _ res ms < <(cprobe open-webui "$T_OK" "github.com:443")
  if [[ ${res:-} == open ]]; then ok "open-webui -> github.com:443 (DNS + egress) (${ms}ms)"
  else bad "open-webui -> github.com:443: ${res:-no-probe}"; fi
else
  skip "open-webui not running"
fi

# --- 6. off-host (manual) -------------------------------------------------------
if (( !PRE )); then
hdr "off-host checks (run on another machine)"
echo "  LAN (should connect):        nc -vz -w 3 ${LAN_IP:-<lan-ip>} 22 3000 8188 9095"
[[ -n $TS_IP ]] && echo "  Tailscale (should connect):  nc -vz -w 3 $TS_IP 22 3000 9095"
echo "  Non-LAN sources are blocked by default; the router's NAT already stops them, so"
echo "  there's nothing to test from outside unless a port is forwarded (it must not be)."
fi

echo
echo "PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
if (( FAIL )); then exit 1; fi
if (( INCONCLUSIVE )); then
  echo "INCONCLUSIVE: the rule-level checks were skipped — run: sudo $0"
  exit 3
fi
exit 0

#!/usr/bin/env bash
# Verify the secure-MCP network lockdown (ADR-0026). Read-only: no renders, no
# restarts, never prints secrets. Runs as brad (needs the docker group) or root.
#
# Proves, from this host, that:
#   - the aichat MCP bridge (:8808) is bound to loopback AND kernel-filtered to
#     localhost (aichat-mcp-bridge.service);
#   - the SECURE ComfyUI MCP bridge (:9001) answers localhost + mcpo (via the internal
#     `mcp-secure` Docker network) but NOT other Docker networks or Tailscale/LAN
#     sources (comfyui-mcp-secure.service IPAddressAllow/IPAddressDeny);
#   - only mcpo is attached to `mcp-secure`;
#   - read-only tool calls still work end-to-end (aichat bridge + mcpo).
#
# Off-host sources are simulated by binding curl to one of this host's own
# non-loopback addresses (curl --interface <ip>): the unit's cgroup filter judges
# that SOURCE address exactly as it would a real LAN/Tailscale/container client.
# A truly remote check still needs another machine — the commands are printed last.
#
# Usage: scripts/verify-mcp-lockdown.sh [--section all|secure|aichat]
#        exit 0 = every check passed (SKIPs allowed), 1 = at least one FAIL.
set -uo pipefail

SECTION=all
case "${1:-}" in
  --section) SECTION="${2:-all}" ;;
  -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1 (see --help)"; exit 2 ;;
esac
[[ $SECTION =~ ^(all|secure|aichat)$ ]] || { echo "bad --section: $SECTION"; exit 2; }

SECURE_UNIT=comfyui-mcp-secure
AICHAT_UNIT=aichat-mcp-bridge
SECURE_PORT=9001
AICHAT_PORT=8808
MCP_NET=mcp-secure
ENV_FILE=/srv/ai/docker/.env
T_OK=5      # timeout for probes that SHOULD answer
T_BLOCK=2   # timeout for probes that must NOT answer (a filtered SYN just times out)

PASS=0 FAIL=0 SKIP=0
ok()   { echo "  PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $*"; SKIP=$((SKIP+1)); }
hdr()  { echo; echo "== $*"; }

# HTTP status of URL from the host ("000" = no HTTP response). Extra curl args after URL.
code() { local url=$1; shift; curl -s -o /dev/null -w '%{http_code}' "$@" "$url" 2>/dev/null; true; }

# Poll until URL gives an HTTP status (any, or exactly $want) or the deadline passes.
wait_http() { # url secs [want]
  local url=$1 secs=$2 want=${3:-} c i
  for ((i = 0; i < secs; i += 2)); do
    c=$(code "$url" -m 2)
    if [[ -n $want && $c == "$want" ]] || [[ -z $want && $c != 000 ]]; then return 0; fi
    sleep 2
  done
  return 1
}

# HTTP status of URL as seen from inside a container ("000" = no response, "n/a" =
# no probe tool in the image or the exec failed).
cprobe() { # container url timeout
  local c=$1 url=$2 t=$3 out
  if docker exec "$c" curl --version >/dev/null 2>&1; then
    out=$(docker exec "$c" curl -s -o /dev/null -w '%{http_code}' -m "$t" "$url" 2>/dev/null)
  elif docker exec "$c" python3 --version >/dev/null 2>&1; then
    out=$(docker exec "$c" python3 -c '
import sys, urllib.request, urllib.error
try:
    print(urllib.request.urlopen(sys.argv[1], timeout=float(sys.argv[2])).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("000")' "$url" "$t" 2>/dev/null)
  fi
  echo "${out:-n/a}"
}

unit_check() { # unit — installed copy matches repo, unit active, filter configured
  local u=$1
  if cmp -s "/srv/ai/scripts/$u.service" "/etc/systemd/system/$u.service"; then
    ok "$u: installed unit matches scripts/$u.service"
  else
    bad "$u: /etc/systemd/system/$u.service differs from the repo copy (re-run its installer)"
  fi
  if systemctl is-active --quiet "$u"; then ok "$u: active"; else bad "$u: not active"; fi
  local allow deny
  allow=$(systemctl show -p IPAddressAllow --value "$u" 2>/dev/null)
  deny=$(systemctl show -p IPAddressDeny --value "$u" 2>/dev/null)
  if [[ -n $deny && -n $allow ]]; then
    ok "$u: IP filter loaded (allow: $allow | deny: $deny)"
  else
    bad "$u: no IPAddressAllow/IPAddressDeny loaded (old unit? run daemon-reload + restart)"
  fi
}

LAN_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
TS_IP=$(ip -4 -o addr show dev tailscale0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)

# --------------------------------------------------------------------------- aichat
if [[ $SECTION == all || $SECTION == aichat ]]; then
  hdr "aichat MCP bridge (:$AICHAT_PORT) — must be localhost-only"
  unit_check "$AICHAT_UNIT"
  wait_http "http://127.0.0.1:$AICHAT_PORT/health" 30 200 || true

  listen=$(ss -Hltn "sport = :$AICHAT_PORT" 2>/dev/null | awk '{print $4}' | sort -u | xargs)
  if [[ -z $listen ]]; then
    bad "nothing listening on :$AICHAT_PORT"
  elif [[ $(echo "$listen" | tr ' ' '\n' | grep -v -E '^(127\.0\.0\.1|\[::1\]):[0-9]+$' | grep -c .) -eq 0 ]]; then
    ok "bound to loopback only: $listen"
  else
    bad "bound beyond loopback: $listen (bridge code missing the MCP_BRIDGE_HOST patch?)"
  fi

  h=$(curl -s -m "$T_OK" "http://localhost:$AICHAT_PORT/health" 2>/dev/null)
  [[ $h == OK ]] && ok "localhost:$AICHAT_PORT/health = OK (what the llm-functions shims use)" \
                 || bad "localhost:$AICHAT_PORT/health did not return OK (got: ${h:-nothing})"

  if [[ -n $LAN_IP ]]; then
    c=$(code "http://$LAN_IP:$AICHAT_PORT/health" -m "$T_BLOCK")
    [[ $c == 000 ]] && ok "LAN IP $LAN_IP:$AICHAT_PORT refuses (no listener)" \
                    || bad "LAN IP $LAN_IP:$AICHAT_PORT answered HTTP $c"
    c=$(code "http://127.0.0.1:$AICHAT_PORT/health" -m "$T_BLOCK" --interface "$LAN_IP")
    [[ $c == 000 ]] && ok "source $LAN_IP -> 127.0.0.1:$AICHAT_PORT dropped (kernel filter)" \
                    || bad "source $LAN_IP -> 127.0.0.1:$AICHAT_PORT answered HTTP $c (IP filter NOT active)"
  else
    skip "no LAN IP found — bind/filter probes from a LAN source skipped"
  fi

  body=$(curl -s -m 30 -X POST "http://127.0.0.1:$AICHAT_PORT/tools/comfyui_secure_get_queue_status" \
           -H 'Content-Type: application/json' -d '{}' 2>/dev/null)
  if echo "$body" | grep -q '"status"'; then
    ok "end-to-end: aichat bridge -> :$SECURE_PORT -> ComfyUI get_queue_status answered"
  else
    bad "end-to-end aichat bridge tool call failed: $(echo "$body" | head -c 160)"
  fi
fi

# --------------------------------------------------------------------------- secure
if [[ $SECTION == all || $SECTION == secure ]]; then
  hdr "secure ComfyUI MCP bridge (:$SECURE_PORT) — localhost + mcpo only"
  unit_check "$SECURE_UNIT"
  wait_http "http://127.0.0.1:$SECURE_PORT/mcp" 60 || true

  envs=$(systemctl show -p Environment --value "$SECURE_UNIT" 2>/dev/null)
  pub_host=$(echo "$envs" | grep -o 'COMFY_MCP_PUBLIC_URL=[^ ]*' | sed -E 's#.*://([^:/]+).*#\1#')
  allow=$(systemctl show -p IPAddressAllow --value "$SECURE_UNIT" 2>/dev/null)
  if [[ -n $pub_host && " $allow " == *" $pub_host/32 "* ]]; then
    ok "COMFY_MCP_PUBLIC_URL host $pub_host is in IPAddressAllow (asset fetches work)"
  else
    bad "COMFY_MCP_PUBLIC_URL host '${pub_host:-?}' not allowlisted as /32 (allow: $allow) — keep them in sync"
  fi
  if [[ -n $LAN_IP && -n $pub_host && $pub_host != "$LAN_IP" ]]; then
    bad "host LAN IP is now $LAN_IP but the unit uses $pub_host (DHCP drift?) — update both"
  fi

  c=$(code "http://127.0.0.1:$SECURE_PORT/mcp" -m "$T_OK")
  [[ $c != 000 ]] && ok "127.0.0.1:$SECURE_PORT answers (HTTP $c; a bare GET is 406)" \
                  || bad "127.0.0.1:$SECURE_PORT not answering — is $SECURE_UNIT up?"

  ai_gw=$(docker network inspect ai -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null)
  for src in ${ai_gw:+"$ai_gw:other-Docker-network"} ${TS_IP:+"$TS_IP:Tailscale"}; do
    ip=${src%%:*} what=${src#*:}
    c=$(code "http://127.0.0.1:$SECURE_PORT/mcp" -m "$T_BLOCK" --interface "$ip")
    [[ $c == 000 ]] && ok "source $ip ($what) -> :$SECURE_PORT dropped (kernel filter)" \
                    || bad "source $ip ($what) -> :$SECURE_PORT answered HTTP $c (IP filter NOT active)"
  done
  [[ -n $TS_IP ]] || skip "tailscale0 has no IPv4 — Tailscale-source probe skipped"

  hdr "Docker: '$MCP_NET' network + container reachability"
  if ! docker info >/dev/null 2>&1; then
    skip "docker not usable by $(id -un) — container checks skipped"
  else
    net=$(docker network inspect "$MCP_NET" \
            -f '{{.Internal}} {{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}' 2>/dev/null)
    read -r internal subnet mcp_gw <<<"$net"
    if [[ -z $net ]]; then
      bad "network '$MCP_NET' missing (run: cd /srv/ai/docker && docker compose up -d --no-deps mcpo)"
    else
      [[ $internal == true ]] && ok "'$MCP_NET' is internal (no default route) — $subnet gw $mcp_gw" \
                              || bad "'$MCP_NET' is NOT internal"
      [[ " $allow " == *" $subnet "* ]] && ok "'$MCP_NET' subnet $subnet is the one $SECURE_UNIT allowlists" \
                                        || bad "'$MCP_NET' subnet $subnet not in $SECURE_UNIT IPAddressAllow ($allow)"
      members=$(docker network inspect "$MCP_NET" -f '{{range .Containers}}{{.Name}} {{end}}' | xargs)
      [[ $members == mcpo ]] && ok "only mcpo is attached to '$MCP_NET'" \
                             || bad "'$MCP_NET' members: '${members:-none}' (must be exactly: mcpo)"
    fi
    mcp_gw=${mcp_gw:-172.31.250.1}

    if docker inspect -f '{{.State.Running}}' mcpo 2>/dev/null | grep -q true; then
      c=$(cprobe mcpo "http://$mcp_gw:$SECURE_PORT/mcp" "$T_OK")
      [[ $c != 000 && $c != n/a && $c != 421 ]] && ok "mcpo -> $mcp_gw:$SECURE_PORT answers (HTTP $c)" \
        || bad "mcpo -> $mcp_gw:$SECURE_PORT gave $c (421 = Host not in COMFY_MCP_ALLOWED_HOSTS)"
      c=$(cprobe mcpo "http://host.docker.internal:$SECURE_PORT/mcp" "$T_BLOCK")
      [[ $c == 000 ]] && ok "mcpo -> host.docker.internal:$SECURE_PORT blocked (old path closed)" \
                      || bad "mcpo -> host.docker.internal:$SECURE_PORT answered $c"
      # Regression: mcpo's OTHER host upstreams still go out via the `ai` network.
      for u in http://host.docker.internal:9000/mcp http://host.docker.internal:9120/mcp \
               http://host.docker.internal:4000/health/liveliness; do
        c=$(cprobe mcpo "$u" "$T_OK")
        [[ $c != 000 && $c != n/a ]] && ok "mcpo -> ${u#http://} still reachable (HTTP $c)" \
                                     || bad "mcpo -> ${u#http://} unreachable ($c) — default route lost?"
      done
    else
      bad "mcpo container not running"
    fi

    # Every other container must fail to reach the bridge, via the mcp-secure gateway
    # IP and via the host's gateway on the container's own network. Probes run in
    # parallel (a blocked probe costs a full timeout).
    tmp=$(mktemp -d)
    for c in $(docker ps --format '{{.Names}}'); do
      [[ $c == mcpo ]] && continue
      nets=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$c")
      [[ " $nets " == *" host "* ]] && { echo "$c host-network" > "$tmp/$c"; continue; }
      gw=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' "$c" | awk '{print $1}')
      ( echo "$c $(cprobe "$c" "http://$mcp_gw:$SECURE_PORT/mcp" "$T_BLOCK") \
              $(cprobe "$c" "http://${gw:-$mcp_gw}:$SECURE_PORT/mcp" "$T_BLOCK") ${gw:-?}" > "$tmp/$c" ) &
    done
    wait
    for f in "$tmp"/*; do
      [[ -e $f ]] || continue
      read -r name a b gw < "$f"
      if [[ $a == host-network ]]; then
        skip "$name uses the host network namespace (it IS localhost by design)"
      elif [[ $a == n/a || $b == n/a ]]; then
        skip "$name: no curl/python3 in the image to probe with"
      elif [[ $a == 000 && $b == 000 ]]; then
        ok "$name cannot reach :$SECURE_PORT (via $mcp_gw and via its gateway $gw)"
      else
        bad "$name reached :$SECURE_PORT (via $mcp_gw: $a, via $gw: $b)"
      fi
    done
    rm -rf "$tmp"
  fi

  hdr "end-to-end via mcpo (read-only get_queue_status)"
  if [[ -r $ENV_FILE ]]; then
    key=$(sed -n 's/^MCPO_API_KEY=//p' "$ENV_FILE" | tr -d "\"'" | head -1)
    if [[ -z $key ]]; then
      skip "MCPO_API_KEY not found in $ENV_FILE"
    elif ! wait_http "http://127.0.0.1:8000/comfyui-secure/openapi.json" 120 200; then
      bad "mcpo never exposed /comfyui-secure (upstream unreachable at mcpo startup?)"
    else
      body=$(curl -s -m 30 -X POST http://127.0.0.1:8000/comfyui-secure/get_queue_status \
               -H "Authorization: Bearer $key" -H 'Content-Type: application/json' -d '{}' 2>/dev/null)
      if echo "$body" | grep -q '"status"'; then
        ok "mcpo /comfyui-secure/get_queue_status answered"
      else
        bad "mcpo comfyui-secure tool call failed: $(echo "$body" | head -c 160)"
      fi
    fi
    unset key
  else
    skip "$ENV_FILE not readable by $(id -un) — mcpo end-to-end skipped"
  fi
fi

echo
echo "Result: $PASS passed, $FAIL failed, $SKIP skipped."
if [[ $SECTION != aichat ]]; then
  echo "Off-host check (from another LAN/tailnet machine) — each must time out or be refused:"
  for ip in $LAN_IP $TS_IP; do
    echo "  curl -m 5 http://$ip:$SECURE_PORT/mcp ; curl -m 5 http://$ip:$AICHAT_PORT/tools"
  done
fi
(( FAIL == 0 ))

#!/usr/bin/env bash
# Install + configure Samba to share /srv/ai over Tailscale + LAN for native
# macOS Finder / Windows Explorer access. See docs/adr/0022-samba-share-srv-ai.md.
#
# Agents cannot sudo -- run this yourself:
#     sudo /srv/ai/scripts/setup-samba.sh
#
# Idempotent: safe to re-run (re-copies config, re-applies ufw rules). It will
# prompt once for the SMB password for user `brad` if one isn't set yet.
#
# What it does:
#   1. apt install samba
#   2. Deploy config/smb.conf -> /etc/samba/smb.conf (backs up any existing file)
#   3. Validate with testparm
#   4. Set an SMB password for `brad` (prompts, first run only)
#   5. Enable + restart smbd (disables the unneeded nmbd/NetBIOS daemon) and
#      check it listens on all addresses (smb.conf filters sources itself)
#
# No firewall changes: scripts/setup-firewall.sh (ADR-0028) already admits the
# LAN (IPv4 + IPv6 ULA/link-local) and the tailnet on every port.
#
set -euo pipefail

REPO=/srv/ai
SRC_CONF="$REPO/config/smb.conf"
DST_CONF=/etc/samba/smb.conf
SHARE_USER=brad

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root (sudo $0)" >&2
  exit 1
fi

if [[ ! -f "$SRC_CONF" ]]; then
  echo "ERROR: $SRC_CONF not found" >&2
  exit 1
fi

if ! id "$SHARE_USER" &>/dev/null; then
  echo "ERROR: unix user '$SHARE_USER' does not exist" >&2
  exit 1
fi

echo "==> Installing samba"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y samba

echo "==> Deploying $DST_CONF"
if [[ -f "$DST_CONF" ]]; then
  cp -a "$DST_CONF" "${DST_CONF}.bak.$(date +%Y%m%d-%H%M%S)"
  echo "    backed up existing config"
fi
install -m 0644 "$SRC_CONF" "$DST_CONF"

echo "==> Validating config (testparm)"
testparm -s "$DST_CONF" >/dev/null

echo "==> SMB account for '$SHARE_USER'"
# pdbedit -L lists existing samba accounts; only prompt if brad isn't one.
if pdbedit -L 2>/dev/null | cut -d: -f1 | grep -qx "$SHARE_USER"; then
  echo "    SMB password already set (skipping). To change it:  sudo smbpasswd $SHARE_USER"
else
  echo "    Set an SMB password for '$SHARE_USER' (separate from the login password):"
  smbpasswd -a "$SHARE_USER"
fi

echo "==> Enabling smbd, disabling unused nmbd (NetBIOS)"
systemctl enable --now smbd
systemctl disable --now nmbd 2>/dev/null || true
systemctl restart smbd

# smb.conf listens on the wildcard addresses (port 445 only). The old per-
# interface binding silently skipped tailscale0, so check what is really bound.
listeners=""
for _ in $(seq 1 10); do
  listeners=$(ss -Hltn '( sport = :445 or sport = :139 )' | awk '{print $4}' | sort | paste -sd' ' -)
  [[ "$listeners" == "0.0.0.0:445 [::]:445" ]] && break
  sleep 1
done
if [[ "$listeners" == "0.0.0.0:445 [::]:445" ]]; then
  echo "    smbd listening on $listeners (sources filtered by 'hosts allow')"
else
  echo "WARNING: expected smbd on '0.0.0.0:445 [::]:445', got '${listeners:-nothing}'" >&2
fi

echo
echo "Done. Mount from a device on the tailnet or LAN:"
echo "  macOS  Finder -> Go -> Connect to Server ->  smb://100.97.143.29/srv-ai"
echo "                                          (or  smb://192.168.4.57/srv-ai )"
echo "  Windows Explorer address bar ->  \\\\100.97.143.29\\srv-ai"
echo "  Log in as '$SHARE_USER' with the SMB password set above."
echo
# Heads-up about root-owned files that brad won't be able to delete over SMB.
ROOT_OWNED=$(find /srv/ai -xdev ! -user "$SHARE_USER" 2>/dev/null | wc -l || echo 0)
if [[ "$ROOT_OWNED" -gt 0 ]]; then
  echo "NOTE: $ROOT_OWNED path(s) under /srv/ai are not owned by '$SHARE_USER' and"
  echo "      can't be modified/deleted over SMB. To fix a specific dir, e.g.:"
  echo "        sudo chown -R $SHARE_USER:$SHARE_USER /srv/ai/comfyui/models/loras"
fi

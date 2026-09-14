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
#   5. Add ufw rules for port 445 on tailscale0 + LAN (does NOT enable ufw)
#   6. Enable + restart smbd (disables the unneeded nmbd/NetBIOS daemon)
#
set -euo pipefail

REPO=/srv/ai
SRC_CONF="$REPO/config/smb.conf"
DST_CONF=/etc/samba/smb.conf
SHARE_USER=brad
LAN_CIDR=192.168.4.0/22   # matches eno1 192.168.4.57/22

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

if command -v ufw &>/dev/null; then
  echo "==> Adding ufw rules for SMB (port 445) on tailnet + LAN"
  # Keep SSH open so enabling ufw later can't lock you out.
  ufw allow 22/tcp                                     >/dev/null || true
  ufw allow in on tailscale0 to any port 445 proto tcp >/dev/null || true
  ufw allow from "$LAN_CIDR" to any port 445 proto tcp >/dev/null || true
  echo "    rules added (ufw NOT auto-enabled; enable manually with: sudo ufw enable)"
fi

echo "==> Enabling smbd, disabling unused nmbd (NetBIOS)"
systemctl enable --now smbd
systemctl disable --now nmbd 2>/dev/null || true
systemctl restart smbd

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

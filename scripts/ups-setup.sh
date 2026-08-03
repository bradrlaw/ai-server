#!/usr/bin/env bash
#
# ups-setup.sh — configure NUT (Network UPS Tools) for the CyberPower CP1500AVRLCD
#                so the AI server shuts down gracefully on an extended outage.
#
# Hardware:  CyberPower CP1500AVRLCD (USB HID Power Device, VID:PID 0764:0601),
#            enumerated by the kernel as hidraw "CPS CP1500AVRLCD3".
# Driver:    NUT usbhid-ups (native, no vendor daemon needed).
# Mode:      standalone (single machine monitors its own UPS over local USB).
# Trigger:   graceful `shutdown -h` on the UPS LOW-BATTERY signal (OB LB) —
#            rides through short blips, powers off only when the battery is
#            nearly empty, maximising uptime.
#
# The agent cannot sudo, so this is handed to the owner to run:
#     sudo /srv/ai/scripts/ups-setup.sh
#
# Idempotent: re-running preserves the generated monitor password and only
# rewrites config if needed. Existing /etc/nut/*.conf are backed up once.
#
set -euo pipefail

UPS_NAME="cyberpower"
UPS_DESC="CyberPower CP1500AVRLCD"
VENDORID="0764"
PRODUCTID="0601"
NUTDIR="/etc/nut"
LISTEN_ADDR="127.0.0.1"
LISTEN_PORT="3493"

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root:  sudo $0" >&2
    exit 1
fi

echo "==> 1/6  Installing NUT (nut-server + nut-client)…"
if ! dpkg -l nut-server 2>/dev/null | grep -q '^ii'; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y nut nut-server nut-client
else
    echo "    NUT already installed — skipping apt."
fi

# NUT's package creates the 'nut' user/group and $NUTDIR.
NUT_GROUP="$(getent group nut >/dev/null && echo nut || echo root)"
mkdir -p "$NUTDIR"

backup_once() {
    local f="$1"
    if [[ -f "$f" && ! -f "$f.orig-preNUTsetup" ]]; then
        cp -a "$f" "$f.orig-preNUTsetup"
        echo "    backed up $(basename "$f") -> $(basename "$f").orig-preNUTsetup"
    fi
}

echo "==> 2/6  Reusing or generating the upsmon password…"
# Preserve the password across re-runs so upsd.users and upsmon.conf stay in sync.
MON_PASS=""
if [[ -f "$NUTDIR/upsd.users" ]]; then
    MON_PASS="$(awk '/^\[upsmon\]/{f=1} f&&/password/{print $NF; exit}' "$NUTDIR/upsd.users" || true)"
fi
if [[ -z "$MON_PASS" ]]; then
    MON_PASS="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    echo "    generated a new random monitor password."
else
    echo "    reusing existing monitor password."
fi

echo "==> 3/6  Writing $NUTDIR/nut.conf (MODE=standalone)…"
backup_once "$NUTDIR/nut.conf"
cat > "$NUTDIR/nut.conf" <<EOF
# Managed by scripts/ups-setup.sh
MODE=standalone
EOF

echo "==> 4/6  Writing ups.conf / upsd.conf / upsd.users…"
backup_once "$NUTDIR/ups.conf"
cat > "$NUTDIR/ups.conf" <<EOF
# Managed by scripts/ups-setup.sh
# Global driver defaults
maxretry = 3

[$UPS_NAME]
    driver = usbhid-ups
    port = auto
    vendorid = $VENDORID
    productid = $PRODUCTID
    desc = "$UPS_DESC"
EOF

backup_once "$NUTDIR/upsd.conf"
cat > "$NUTDIR/upsd.conf" <<EOF
# Managed by scripts/ups-setup.sh
# Bind the data server to loopback only (single-machine standalone).
LISTEN $LISTEN_ADDR $LISTEN_PORT
EOF

backup_once "$NUTDIR/upsd.users"
cat > "$NUTDIR/upsd.users" <<EOF
# Managed by scripts/ups-setup.sh
[upsmon]
    password = $MON_PASS
    upsmon primary
EOF

echo "==> 5/6  Writing upsmon.conf (shut down on LOW BATTERY)…"
backup_once "$NUTDIR/upsmon.conf"
cat > "$NUTDIR/upsmon.conf" <<EOF
# Managed by scripts/ups-setup.sh
#
# Monitor the local UPS. Power value 1 = this UPS feeds 1 power supply that we
# require to stay up (MINSUPPLIES 1). upsmon runs as 'primary' since it is
# directly attached to the UPS over USB.
MONITOR $UPS_NAME@localhost 1 upsmon $MON_PASS primary

MINSUPPLIES 1

# On a fatal condition (UPS on battery AND low battery, i.e. "OB LB"),
# upsmon runs this to bring the box down cleanly. Give a short grace so
# systemd can stop containers, unload GPUs and flush disks.
SHUTDOWNCMD "/sbin/shutdown -h +0 'UPS low battery — graceful shutdown'"

POLLFREQ 5
POLLFREQALERT 5
HOSTSYNC 15
DEADTIME 15
POWERDOWNFLAG /etc/killpower
FINALDELAY 5

# Log/notify on state changes (syslog only; no wall spam on a headless box).
NOTIFYFLAG ONLINE   SYSLOG
NOTIFYFLAG ONBATT   SYSLOG+WALL
NOTIFYFLAG LOWBATT  SYSLOG+WALL
NOTIFYFLAG FSD      SYSLOG+WALL
NOTIFYFLAG SHUTDOWN SYSLOG
NOTIFYFLAG COMMBAD  SYSLOG
NOTIFYFLAG COMMOK   SYSLOG
EOF

echo "==> 5b   Locking down permissions (files hold a password)…"
chown root:"$NUT_GROUP" "$NUTDIR"/nut.conf "$NUTDIR"/ups.conf "$NUTDIR"/upsd.conf \
    "$NUTDIR"/upsd.users "$NUTDIR"/upsmon.conf
chmod 640 "$NUTDIR"/nut.conf "$NUTDIR"/ups.conf "$NUTDIR"/upsd.conf \
    "$NUTDIR"/upsd.users "$NUTDIR"/upsmon.conf

echo "==> 5c   Applying NUT udev rules to the (already-plugged) UPS…"
# NUT ships /usr/lib/udev/rules.d/62-nut-usbups.rules which chgrps the USB node
# to group 'nut'. Those rules only fire on a plug event, so if the UPS was
# already connected before NUT installed, the existing /dev/bus/usb node stays
# root:root and the driver fails with "insufficient permissions on everything".
# Reload + re-trigger for this vendor to fix it without a replug/reboot.
udevadm control --reload-rules || true
udevadm trigger --action=add --subsystem-match=usb \
    --attr-match=idVendor="$VENDORID" || true
sleep 1
UPSNODE="$(lsusb | awk -v v="$VENDORID" -v p="$PRODUCTID" \
    'tolower($6)==(v":"p){printf "/dev/bus/usb/%s/%s",$2,substr($4,1,3)}')"
if [[ -n "${UPSNODE:-}" ]]; then
    echo "    UPS USB node: $UPSNODE ($(stat -c '%U:%G %a' "$UPSNODE" 2>/dev/null))"
fi

echo "==> 6/6  Enabling + (re)starting NUT services…"
# nut-driver-enumerator turns ups.conf sections into nut-driver@ instances.
systemctl daemon-reload
systemctl enable nut-driver-enumerator.service nut-server.service nut-monitor.service >/dev/null 2>&1 || true
systemctl restart nut-driver-enumerator.service || true
# Restart the per-UPS driver instance so it re-opens the (now group-nut) node.
systemctl reset-failed "nut-driver@$UPS_NAME.service" 2>/dev/null || true
systemctl restart "nut-driver@$UPS_NAME.service" || true
systemctl restart nut-server.service
systemctl restart nut-monitor.service

sleep 3
echo
echo "=========================================================================="
echo " UPS status (upsc $UPS_NAME):"
echo "=========================================================================="
if upsc "$UPS_NAME" 2>/dev/null; then
    echo
    echo "SUCCESS — NUT is talking to the UPS."
    echo "Key fields to sanity-check:"
    upsc "$UPS_NAME" 2>/dev/null | grep -E \
      'ups.status|battery.charge|battery.runtime|ups.load|input.voltage' || true
    echo
    echo "Test (do NOT do this casually — it simulates a power event):"
    echo "    upsmon -c fsd        # force a shutdown drill"
    echo "    upsc $UPS_NAME ups.status   # OL = online, OB = on battery, LB = low"
else
    echo "WARNING — could not query the UPS yet. Check:"
    echo "    systemctl status nut-server nut-monitor 'nut-driver@*'"
    echo "    journalctl -u nut-server -u 'nut-driver@*' -b --no-pager | tail -40"
    echo "    lsusb | grep -i cyber"
fi
echo "=========================================================================="

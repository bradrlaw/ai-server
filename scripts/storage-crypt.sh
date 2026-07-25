#!/usr/bin/env bash
#
# storage-crypt.sh — manage the encrypted /srv/ai/storage volume.
#
# Backing device: ADATA SU760 500GB, addressed by its stable serial-based
# by-id path so shuffling of /dev/sdX letters can never target the wrong disk.
# Encryption:     LUKS2 (whole-disk, no partition table), manual passphrase.
# Filesystem:     ext4, mounted at /srv/ai/storage.
# Unlock policy:  MANUAL over SSH after each reboot (headless server); the
#                 volume is intentionally NOT auto-mounted at boot.
#
# Usage (run as root):
#   sudo scripts/storage-crypt.sh setup    # ONE-TIME, DESTRUCTIVE: format + mkfs
#   sudo scripts/storage-crypt.sh unlock   # open LUKS + mount (prompts passphrase)
#   sudo scripts/storage-crypt.sh lock      # umount + close LUKS
#   sudo scripts/storage-crypt.sh status    # show current state
#
set -euo pipefail

DISK="/dev/disk/by-id/ata-ADATA_SU760_2L4529QQK2KR"
MAPPER="aidata"
MAPPER_DEV="/dev/mapper/${MAPPER}"
MOUNT="/srv/ai/storage"

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
  fi
}

require_disk() {
  if [[ ! -b "${DISK}" ]]; then
    echo "error: backing disk not found at ${DISK}" >&2
    echo "       is the ADATA SU760 connected? check: ls -l /dev/disk/by-id/ | grep SU760" >&2
    exit 1
  fi
}

cmd_setup() {
  require_root
  require_disk
  echo "=========================================================================="
  echo " DESTRUCTIVE: this will ERASE all data on:"
  echo "   ${DISK}"
  echo "   ($(readlink -f "${DISK}"), $(lsblk -dno SIZE "$(readlink -f "${DISK}")" | tr -d ' '))"
  echo "=========================================================================="
  read -r -p "Type ERASE to continue: " confirm
  [[ "${confirm}" == "ERASE" ]] || { echo "aborted."; exit 1; }

  echo ">> wiping old signatures + partition table"
  wipefs -a "${DISK}"

  echo ">> creating LUKS2 container (you will set the passphrase now)"
  cryptsetup luksFormat --type luks2 "${DISK}"

  echo ">> opening container (enter the passphrase you just set)"
  cryptsetup open "${DISK}" "${MAPPER}"

  echo ">> creating ext4 filesystem"
  mkfs.ext4 -L aidata -m 0 "${MAPPER_DEV}"

  echo ">> mounting at ${MOUNT}"
  mkdir -p "${MOUNT}"
  mount "${MAPPER_DEV}" "${MOUNT}"

  LUKS_UUID="$(cryptsetup luksUUID "${DISK}")"
  echo
  echo "SETUP COMPLETE."
  echo "  LUKS UUID: ${LUKS_UUID}"
  echo "  mounted:   ${MOUNT}"
  echo
  echo "Add a fstab entry (noauto,nofail so a missing/locked disk never blocks boot):"
  echo "  echo 'LABEL=aidata ${MOUNT} ext4 defaults,noauto,nofail 0 2' | sudo tee -a /etc/fstab"
  echo
  echo "From now on, after each reboot unlock with:"
  echo "  sudo $(realpath "$0") unlock"
}

cmd_unlock() {
  require_root
  require_disk
  if [[ ! -b "${MAPPER_DEV}" ]]; then
    echo ">> opening LUKS container (enter passphrase)"
    cryptsetup open "${DISK}" "${MAPPER}"
  else
    echo ">> ${MAPPER_DEV} already open"
  fi
  mkdir -p "${MOUNT}"
  if mountpoint -q "${MOUNT}"; then
    echo ">> ${MOUNT} already mounted"
  else
    echo ">> mounting ${MOUNT}"
    mount "${MAPPER_DEV}" "${MOUNT}"
  fi
  # Recreate filebrowser so its bind-mounts re-resolve the now-present
  # symlinked secure dirs (/srv/ai/comfyui/{input,output}-secure ->
  # /srv/ai/storage/...). Docker resolves bind sources at create time, so a
  # plain restart is not enough. Best-effort: never fail the unlock over this.
  if command -v docker >/dev/null 2>&1 && [[ -f /srv/ai/docker/docker-compose.yml ]]; then
    if docker compose version >/dev/null 2>&1; then
      echo ">> recreating filebrowser to pick up encrypted-volume mounts"
      ( cd /srv/ai/docker && docker compose up -d --force-recreate filebrowser ) \
        || echo "   warning: filebrowser recreate failed (recreate it manually)" >&2
    fi
  fi

  echo "unlocked."
  cmd_status
}

cmd_lock() {
  require_root
  if mountpoint -q "${MOUNT}"; then
    echo ">> unmounting ${MOUNT}"
    umount "${MOUNT}"
  fi
  if [[ -b "${MAPPER_DEV}" ]]; then
    echo ">> closing LUKS container"
    cryptsetup close "${MAPPER}"
  fi
  echo "locked."
}

cmd_status() {
  echo "disk:    ${DISK} -> $(readlink -f "${DISK}" 2>/dev/null || echo MISSING)"
  if [[ -b "${MAPPER_DEV}" ]]; then
    echo "luks:    OPEN (${MAPPER_DEV})"
  else
    echo "luks:    closed"
  fi
  if mountpoint -q "${MOUNT}" 2>/dev/null; then
    echo "mount:   MOUNTED at ${MOUNT}"
    df -h "${MOUNT}" | tail -1 | awk '{print "         used "$3" / "$2" ("$5")"}'
  else
    echo "mount:   not mounted"
  fi
}

case "${1:-}" in
  setup)  cmd_setup ;;
  unlock) cmd_unlock ;;
  lock)   cmd_lock ;;
  status) cmd_status ;;
  *) echo "usage: sudo $0 {setup|unlock|lock|status}" >&2; exit 1 ;;
esac

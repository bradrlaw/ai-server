#!/usr/bin/env bash
#
# backup-clone.sh — keep a bootable, whole-disk backup of the Intel boot NVMe
# on the spare WD Green 2TB (ADR-0038).
#
#   sudo scripts/backup-clone.sh clone   # ONE-TIME, DESTRUCTIVE: wipe the WD, copy everything
#   sudo scripts/backup-clone.sh sync    # incremental refresh of the root fs (weekly timer)
#   sudo scripts/backup-clone.sh status  # last sync + target layout
#
# clone: replicates the partition table (fresh GUIDs, so both disks can be
# attached at once), ntfsclones the Windows partitions, copies the ESP, makes
# ext4 root (label aibackup) + rsync of the live /, rewrites fstab, installs grub.
# sync:  rsync -x / -> aibackup (fstab, grub.cfg and swap file are NOT synced),
# then re-runs update-grub in a chroot.
#
# The clone gets NEW filesystem UUIDs; its fstab/grub point at them. Boot it by
# picking the WD in the firmware boot menu (see docs/server-setup.md).
set -euo pipefail

SRC_DISK="/dev/disk/by-id/nvme-INTEL_SSDPEDME020T4_CVMD6361008W2P0EGN"
DST_DISK="/dev/disk/by-id/nvme-WD_Green_SN350_2TB_254178800810"
ROOT_LABEL="aibackup"
MNT="/mnt/backup-clone"
STATE_DIR="/var/lib/backup-clone"
LOCK="/run/backup-clone.lock"

# Not copied: pseudo/transient dirs and the swap file (recreated on the clone).
# fstab and grub.cfg are clone-specific, so they are never overwritten.
EXCLUDES=(
  --exclude=/proc/* --exclude=/sys/* --exclude=/dev/* --exclude=/run/*
  --exclude=/tmp/* --exclude=/mnt/* --exclude=/media/* --exclude=/lost+found
  --exclude=/swap.img --exclude=/etc/fstab --exclude=/boot/grub/grub.cfg
)

die() { echo "error: $*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die "must run as root (use sudo)"
exec 9>"${LOCK}"; flock -n 9 || die "another backup-clone is running"

part() { echo "$(readlink -f "${DST_DISK}")p$1"; }

check_disks() {
  [[ -b ${SRC_DISK} && -b ${DST_DISK} ]] || die "source or target disk not found"
  local s d
  s=$(readlink -f "${SRC_DISK}"); d=$(readlink -f "${DST_DISK}")
  [[ ${s} != "${d}" ]] || die "source and target are the same disk"
  [[ $(findmnt -no SOURCE /) == "${s}"p* ]] || die "/ is not on ${s}; refusing"
  [[ $(blockdev --getsize64 "${d}") -ge $(blockdev --getsize64 "${s}") ]] \
    || die "target is smaller than source"
  ! lsblk -nro MOUNTPOINT "${d}" | grep -q . || die "target has mounted partitions"
}

mounts_up() {
  mkdir -p "${MNT}"
  mount "$(part 4)" "${MNT}"
  mkdir -p "${MNT}/boot/efi"
  mount "$(part 1)" "${MNT}/boot/efi"
}

chroot_grub() { # $1 = "install" to also (re)install the bootloader
  for d in dev dev/pts proc sys; do mount --bind "/${d}" "${MNT}/${d}"; done
  mount --bind /sys/firmware/efi/efivars "${MNT}/sys/firmware/efi/efivars" 2>/dev/null || true
  local rc=0
  {
    if [[ ${1:-} == install ]]; then
      chroot "${MNT}" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
        --removable --no-nvram
    fi
    chroot "${MNT}" update-grub
  } || rc=$?
  umount -R "${MNT}/dev" "${MNT}/proc" "${MNT}/sys" 2>/dev/null || true
  return ${rc}
}

mounts_down() { umount -R "${MNT}" 2>/dev/null || true; }

do_rsync() {
  nice -n 19 ionice -c3 rsync -aHAXx --numeric-ids --delete --info=stats1 \
    "${EXCLUDES[@]}" "$@" / "${MNT}/"
}

cmd_clone() {
  check_disks
  local s d old_root old_esp new_root new_esp
  s=$(readlink -f "${SRC_DISK}"); d=$(readlink -f "${DST_DISK}")
  echo "About to ERASE ${d} ($(lsblk -ndo MODEL,SIZE "${d}")) and clone ${s} onto it."
  read -r -p "Type ERASE to continue: " ans; [[ ${ans} == ERASE ]] || die "aborted"

  echo ">> partition table (new GUIDs)"
  wipefs -a "${d}" >/dev/null
  sgdisk --zap-all "${d}" >/dev/null
  sgdisk --replicate="${d}" "${s}" >/dev/null
  sgdisk -G "${d}" >/dev/null
  partprobe "${d}"; udevadm settle; sleep 2

  echo ">> Windows partitions (p3 NTFS, p5 recovery NTFS), p2 MSR raw"
  dd if="${s}p2" of="${d}p2" bs=1M status=none
  for n in 3 5; do
    ntfsclone --force --overwrite "${d}p${n}" "${s}p${n}" \
      || echo "WARN: ntfsclone p${n} failed (dirty/hibernated NTFS?) - Windows copy incomplete"
  done

  echo ">> ESP + root filesystems"
  old_root=$(blkid -s UUID -o value "${s}p4"); old_esp=$(blkid -s UUID -o value "${s}p1")
  mkfs.vfat -F32 "${d}p1" >/dev/null
  mkfs.ext4 -F -q -L "${ROOT_LABEL}" "${d}p4"
  new_root=$(blkid -s UUID -o value "${d}p4"); new_esp=$(blkid -s UUID -o value "${d}p1")
  trap mounts_down EXIT
  mounts_up
  rsync -rt --no-owner --no-group --modify-window=1 /boot/efi/ "${MNT}/boot/efi/"

  echo ">> root rsync (~1 TB; slow on this drive)"
  do_rsync
  sed -i "s/${old_root}/${new_root}/g; s/${old_esp}/${new_esp}/g" "${MNT}/etc/fstab"
  grep -rlE "${old_root}|${old_esp}" "${MNT}/boot/efi" --include='*.cfg' 2>/dev/null \
    | xargs -r sed -i "s/${old_root}/${new_root}/g; s/${old_esp}/${new_esp}/g"
  fallocate -l "$(stat -c %s /swap.img)" "${MNT}/swap.img"; chmod 600 "${MNT}/swap.img"
  mkswap "${MNT}/swap.img" >/dev/null

  echo ">> bootloader"
  chroot_grub install
  mounts_down; trap - EXIT
  stamp
  echo "Done. Clone root UUID ${new_root}. Test: reboot, pick the WD in the boot menu."
}

cmd_sync() {
  check_disks
  local dev; dev=$(blkid -L "${ROOT_LABEL}") || die "no ${ROOT_LABEL} filesystem; run clone first"
  [[ $(readlink -f "${dev}") == "$(part 4)" ]] || die "${ROOT_LABEL} is not on the backup disk"
  trap mounts_down EXIT
  mounts_up
  grep -q "$(blkid -s UUID -o value "$(part 4)")" "${MNT}/etc/fstab" \
    || die "clone fstab does not reference its own UUID; refusing to sync"
  do_rsync "$@"
  [[ " $* " == *" -n "* || " $* " == *" --dry-run "* ]] || { chroot_grub; stamp; }
  mounts_down; trap - EXIT
  echo "sync complete"
}

stamp() { mkdir -p "${STATE_DIR}"; date -u +%FT%TZ >"${STATE_DIR}/last-sync"; }

cmd_status() {
  echo "last sync: $(cat "${STATE_DIR}/last-sync" 2>/dev/null || echo never)"
  lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID "$(readlink -f "${DST_DISK}")"
}

case "${1:-}" in
  clone) cmd_clone ;;
  sync) shift; cmd_sync "$@" ;;
  status) cmd_status ;;
  *) sed -n 3,9p "$0"; exit 2 ;;
esac

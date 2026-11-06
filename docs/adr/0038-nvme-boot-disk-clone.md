# ADR-0038: Bootable clone of the boot NVMe on the WD Green 2TB

- Status: Proposed (2026-11-06)

## Context
The Intel DC P3600 2TB (`nvme0n1`) is the only boot disk and holds `/` plus two Windows
partitions. A spare, identically sized WD Green SN350 2TB (`nvme1n1`) is installed. The
owner wants it as a fail-over so a dead Intel SSD does not need a rebuild. Existing backups
(see "Backups" in `docs/server-setup.md`) are manual Windows-side images.

## Decision
`scripts/backup-clone.sh` replicates the partition table with fresh GUIDs (both disks are
attached at once), ntfsclones the Windows partitions, copies the ESP, and rsyncs the live
root onto an ext4 filesystem labelled `aibackup`, then fixes fstab and installs GRUB
(`--removable`). A weekly systemd timer (`install-backup-clone.sh`) re-syncs the root with
`rsync -aHAXx --delete`; fstab, `grub.cfg` and the swap file are never overwritten.
Boot the clone from the firmware boot menu.

## Consequences
- Crash-consistent only (live rsync), not a snapshot; no history/retention.
- Windows clone is a one-time copy and may need a BCD repair when booted from the WD.
- Encrypted storage and the bulk HDD are not covered.
- The WD is DRAM-less QLC: slow sustained writes, low endurance; weekly deltas are small.
- Untested boot: verify once by rebooting into the WD after the first clone.

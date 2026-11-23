# ADR-0040: Manual OS updates only; NVIDIA driver held; checked kernel upgrades

- **Status:** Accepted (policy in force since the July 2026 incident; written up 2026-11-23)
- **Date:** 2026-11-23
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
On 2026-07-25 at 07:01 UTC, `unattended-upgrades` bumped the NVIDIA driver from 580.159.03 to
580.173.02 on the running server. The loaded kernel module stayed at 580.159 while NVML and `nvidia-smi`
moved to 580.173, so every NVML call failed with "Driver/library version mismatch". The fan daemon
could not read GPU temperatures (fans ramped to their failsafe) and no GPU work could start until a
reboot loaded the new module. The same July window had also installed a new kernel (6.8.0-136 on
07-18), Python 3.12, OpenSSH and others, all silently, on a box whose GPU stack (Maxwell + Volta,
CUDA 12.x only, ADR-0002) is easy to break. The owner chose "hold the NVIDIA driver AND disable
unattended-upgrades entirely" right after the incident. This was only recorded in the agent memory and as an
aside in ADR-0017, not in the repo.

Since then the box has drifted (164 pending updates by 2026-11-23, including a kernel CVE fix), so
"no automatic updates" also needs a safe way to do *manual* ones.

## Decision
1. **No automatic updates.** `APT::Periodic::*` is `"0"` in `/etc/apt/apt.conf.d/20auto-upgrades`;
   `unattended-upgrades.service`, `apt-daily.timer` and `apt-daily-upgrade.timer` are disabled. The
   Copilot CLI's own updater is off (`COPILOT_AUTO_UPDATE=false` in `~/.bashrc`).
2. **The NVIDIA 580-server driver stack is `apt-mark hold`ed:** all 16 `*580-server*` packages
   (`dpkg-query -W -f='${Package}\n' | grep 580-server`), currently 580.173.02.
   `nvidia-container-toolkit` is version-independent and not held. A driver change is its own
   deliberate step (unhold, upgrade, reboot right away), never part of a routine update.
3. **The kernel is not held.** Kernel security fixes still matter, and DKMS rebuilds the held driver's
   module for each new kernel when it is installed. Kernels are upgraded by hand with the runbook
   procedure ("OS updates and kernel upgrades" in `docs/server-setup.md`). Its guard rails are the
   read-only `scripts/os-update-check.sh`:
   - `pre` (after `sudo apt update`): auto-updates still off, every driver package held, loaded
     module == userspace, and a simulated `apt full-upgrade` that fails if a held 580-server package,
     a different NVIDIA driver or a CUDA 13 toolkit would be touched (ADR-0002). It lists the new
     kernel, removals, and GPU/container-relevant updates (dkms, docker, containerd, cuDNN, the
     container toolkit) for review.
   - `ready` (after the upgrade, before rebooting): the NVIDIA module is built at the held version
     for every installed kernel, and the kernel that will boot has headers.
   - `post` (after the reboot): module == userspace, 3 GPUs, fan daemon fresh, core services and
     containers up, storage unlocked.
4. **Keep a way back.** Refresh the boot-disk clone (ADR-0038) before upgrading, keep the previous
   kernel installed (no `apt autoremove` until the new one has run cleanly for a few days), and
   reboot straight after a kernel or driver change so module and userspace never disagree.

## Consequences
- Positive: no surprise driver/library mismatch, no silent kernel change under a long GPU job, and
  updates happen in a planned window with models unloaded.
- Negative: security fixes only land when the owner runs them. The audit on 2026-11-23 found 164
  pending updates; a monthly update window (plus out-of-band runs for exploited CVEs) is the
  counterweight.
- `dkms` comes from NVIDIA's CUDA repo (its pin-600 outranks Ubuntu's), so a routine upgrade can move
  `dkms` itself; the `ready` check catches a failed module build before the reboot.
- GRUB is `GRUB_DEFAULT=0`, `GRUB_TIMEOUT=0` (hidden menu) on a headless box, so picking the old
  kernel needs either SSH (set `GRUB_DEFAULT` to the old entry + `update-grub`) or a console; if the
  new kernel can't reach the network, boot the WD clone from the UEFI menu.

## Alternatives considered
- **Hold the kernel as well** - rejected: kernel CVEs (e.g. CVE-2026-98432, fixed in 6.8.0-149) would
  then never land, and DKMS already handles the module rebuild.
- **Keep `unattended-upgrades` with the driver blacklisted** - rejected by the owner: it still restarts
  services and installs kernels behind long-running GPU jobs, and the July incident cost trust.
- **Livepatch** - kernel livepatches don't cover every CVE, need an Ubuntu Pro token on the box, and
  still need reboots for the rest; not worth it for a single home server.

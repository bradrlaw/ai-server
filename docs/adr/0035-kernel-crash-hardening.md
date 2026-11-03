# ADR-0035: Kernel crash hardening (MGLRU off, panic-on-oops reboot)

- **Status:** Accepted (files tracked; the owner runs `sudo scripts/install-crash-hardening.sh`)
- **Date:** 2026-11-03
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
First hard hang since setup. Journal of the dead boot (ends 06:04:42 UTC) shows `kswapd0` hitting
`UBSAN: array-index-out-of-bounds` in `mm/vmscan.c:4286` (`sort_folio`, index -1 into `list_head[4][2][5]`)
and then a page fault at `00000001ffffffff` in `scan_folios`: the multi-gen LRU (MGLRU) reclaim path of Ubuntu
kernel 6.8.0-136 (tainted P/OE by NVIDIA). `kswapd0` died holding LRU state; later writers soft-locked on the
LRU spinlock (CPU#15 stuck 26 s) and the box wedged until power-cycled. The wifi/DNS errors on the console were
fallout, not the cause. The time was 01:03 EST. **Trigger:** the Hermes `v100-perf-scout` cron (`0 1 * * 0,2,4,6`,
model `big`) fired at 06:00:23 UTC while an overnight Copilot session was using `flash`. llama-swap evicted `flash`
for `big` (06:00:23), then the next Copilot request evicted `big` to reload `flash` (06:01:38), so Strata
(about 72 GB pinned host RAM, 64 GB page cache) was torn down and restarted under heavy page-cache churn about
100 s before `kswapd0` faulted. The kernel bug is the fault; the flash/big thrash is what exercised reclaim.
Bad RAM is not ruled out, but this is the first crash in about 4 months. EDAC reports no memory controllers
(non-ECC board), so memory errors will not appear there; only memtest can test the RAM.

## Decision
1. Disable MGLRU at boot: `config/tmpfiles/lru-gen-off.conf` writes `0` to `/sys/kernel/mm/lru_gen/enabled`.
2. `config/sysctl/90-ai-server-crash.conf`: `kernel.panic_on_oops=1`, `softlockup_panic=1`, `hung_task_panic=1`,
   `hung_task_timeout_secs=300`, `kernel.panic=10`, so a kernel fault reboots in 10 s (services come back via
   systemd) instead of leaving a headless box hung. A panic also lets `efi_pstore` keep the trace.
3. Installer: `scripts/install-crash-hardening.sh` (needs sudo).
4. Stop the flash/big thrash: new LiteLLM virtual model `v100-auto` (`docker/litellm/config.yaml`; routing in
   `custom_hooks.py::_pick_v100_model`). It asks llama-swap `/running` and routes to `flash` if loaded, else `big`
   (loading `big` if neither is; any lookup failure falls back to `big`). The Hermes `v100-perf-scout` and
   `model-engine-scout` crons in `hermes/cron/jobs.json` (gitignored; backup `jobs.json.bak-20261103`) now use it,
   so a scout never evicts a running `flash` session.

## Consequences
- A scout that runs while `flash` is loaded uses `flash`, which is fine for research but queues behind an
  interactive session on its single slot.
- Slightly less efficient reclaim (classic LRU); negligible for this workload.
- Any kernel oops now costs a reboot (about 1-2 min plus model warm-up) rather than a hang; in-flight requests fail.
- A soft lockup or hung task now also reboots; if that false-triggers under heavy prefill, relax
  `hung_task_timeout_secs` or drop `hung_task_panic`.

## Follow-ups
- If it recurs with MGLRU off: run memtest86+ / `memtester`, check `edac-util -s` and `ras-mc-ctl --errors`,
  and look at a newer 6.8.0-1xx kernel (mind the matching NVIDIA module build).
- kdump is not installed; pstore only keeps a short log tail.

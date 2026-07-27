# ADR-0017 — P100 HBM2 ECC death: diagnosis and Titan X stopgap swap

* Status: Accepted
* Date: 2026-07-27
* Deciders: @bradrlaw (+ Copilot CLI)

## Context

The Tesla P100-16GB (idx0, bus01, sm_60) that served the `fast` / `fast-uncensored`
carve-out (ADR-0014) died from an **uncorrectable HBM2 memory failure**. The
symptoms first looked like a software/driver regression, so the diagnosis path is
worth recording — the failure mode wedges the *entire* NVIDIA stack, not just the
bad card.

UUID `GPU-d85ec299-860f-c3e8-1546-64c7d79a2b47`, board serial `0322417146308`.

### Symptoms (in the order they appeared)
1. `nvidia-smi dmon` / the fan-control and status-page pollers pegged a CPU core;
   system load climbed to ~7 and the box felt sluggish.
2. Every new `nvidia-smi` invocation hung in uninterruptible `D` state and stacked
   up; the status page showed the two V100s as "unavailable".
3. gpu-fan-control log: `power cap GPU0 -> 200W FAILED ... Unknown Error`;
   `power.draw` for idx0 read `[Unknown Error]` while temp/limit still read fine.
4. A monitoring `nvidia-smi` appeared to "respawn" — actually the timer-driven
   status-page poller re-launching, each instance hanging on the dead card.

### Root cause (confirmed via dmesg after a reboot)
```
Xid 62  — GPU microcontroller (µC) halt
Xid 48  — uncorrectable double-bit ECC error (DBE) in framebuffer, partition 1/1
Xid 171 — HBM, uncorrectable DRAM error in FBPA 1 subpartition 1
Xid 64  — Dynamic Page Retirement: FATAL, unable to retire page (0x3fdf5e)
Xid 154 — GPU recovery action changed None -> "GPU Reset Required"
```
The HBM2 took a double-bit error at a physical address the driver then **could not
retire** (retirement table full/failed), which halted the on-die microcontroller.
With the µC halted, NVML can no longer talk to the card: `power.draw` returns
`Unknown Error`, and because NVML holds a **global lock**, one wedged GPU makes
*every* `nvidia-smi` / `cuInit` on the system hang — hence the CPU spin, the load
climb, and the V100s appearing "unavailable" even though they were fine.

### Why it was NOT the driver / cooling (ruled out during triage)
* **Driver:** an unattended-upgrade on 2026-07-25 had bumped NVIDIA 580.159.03 →
  580.173.02, which looked like a smoking gun. But per-boot fan-control logs proved
  the P100 ran clean on 580.173.02 for two days (including the morning of the
  failure); "Unknown Error" first appeared only at a later reboot. Same driver
  before and after ⇒ not a software regression. (Auto-updates have since been
  disabled and the driver `apt-mark hold`ed regardless — see the auto-update memory.)
* **Cooling:** our thermals were fine. The P100 never throttled — it settled
  ~70-75 °C at the 200 W cap with the pump-fan curve, ~15 °C under limit, and the
  fan was operating normally right up to the failure. (When physically pulled the
  card was quite hot to the touch, but that is post-mortem heat soak, not a cooling
  regression — the sensor/curve data show no thermal event.) This was a **silicon
  HBM failure**, not a thermal one.

### Why a reset couldn't fix it
`nvidia-smi -r -i 0` requires a live microcontroller to accept the reset. Xid 62
means the µC is already halted, so the reset itself hangs. The halted state lives
in-silicon and survives reboots, reseats, and full power-cuts — reseating the card
and re-seating every power connector changed nothing. The card is not recoverable.

## Decision

1. **Retire the P100** as failed hardware.
2. **Install a 12 GB GTX Titan X (Maxwell, sm_52)** into the same bus01 slot as a
   temporary stopgap so the server keeps three GPUs and the `fast` slot stays alive.
3. **Rebuild llama.cpp for the arch superset `52;60;61;70`** (was `60;70`) so the
   same binary still runs a future P100/V100 *and* the Maxwell Titan X. The prior
   working `60;70` build is preserved at
   `src/llama.cpp/build.bak-sm60_70-4f31eedb0-20260727/` for when a passive card
   returns.
4. **Repoint the `fast` slot** from the 16 GB Gemma-4-26B-A4B MoE to the dense
   **Gemma-4-12B** (Q4_K_XL, 32k ctx) so it fits the Titan X's 12 GB. The MoE
   block is preserved commented-out in `config/llama-swap.base.yaml` for restore.
   LiteLLM's `fast` entry is unchanged (same model id), so no gateway edit. The
   other two idx0 models (`fast-12b`, `fast-uncensored`) were likewise dropped
   from 128k ctx to 32k (128k OOMs on 12 GB).
   **The MTP speculative draft (`--spec-type draft-mtp`) was stripped from all
   three idx0 models** — it crashes on the Maxwell Titan X with a CUDA illegal
   memory access in `common_speculative_impl_draft_mtp::draft` (the draft context
   also fails to initialise: "Gemma4Assistant requires ctx_other to be set"). The
   crash only appears on a real completion request, not at model load / health
   check, which is why the initial `llama-bench` smoke test (no draft) missed it
   and the symptom was "GPU activity in Open WebUI but no output". Without the
   draft the models serve correctly (just slower decode).
5. **Adapt gpu-fan-control** for the Titan X: it cools itself with its own
   built-in fan (readable via `nvidia-smi fan.speed`, unlike the passive
   P100/V100 which report `N/A`) and does **not** use the chassis PUMP_FAN. So the
   idx0 zone is made **`monitor_only`**: the daemon logs the card's temp and its
   own fan duty (as `gpufan%`) but drives no chassis PWM, and hands the old P100
   pump-fan header (pwm1) back to BIOS auto at startup so we stop forcing an unused
   fan. The V100 zones are unchanged.

## Consequences

* Positive: server stays fully operational on three GPUs; `coding`+`chat` on the
  V100s are untouched; the `fast` slot still serves (12B on the Titan X:
  ~pp128 88 t/s / tg32 23 t/s, 12.2 GB VRAM). One binary now covers Maxwell→Volta.
* Negative / trade-offs: the Titan X is slower than the P100 was and only 12 GB, so
  `fast` dropped from the 26B MoE @ 128k ctx to a 12B dense @ 32k ctx. It is a
  consumer Maxwell card (no ECC, older, no ADR-0014 fp16 accuracy patch relevance).
  The `big` dual-V100 model lost its NCCL build during the rebuild (minor; single-
  GPU models unaffected) — revisit if `big` matters.
* Follow-ups / things to watch:
  - When a 16 GB+ passive card (another P100, or a V100) returns to idx0: restore
    the MoE `fast` block, revert the fan-zone label/curve, and optionally rebuild
    off `build.bak-sm60_70-...`. Re-enable NCCL if `big` is wanted.
  - Operational lesson: a single wedged GPU hangs the *whole* NVML stack. If
    `nvidia-smi` hangs and `power.draw` reads `Unknown Error` on one card, suspect
    that card's silicon (check dmesg for Xid 48/62/64/171/154) and physically pull
    it — don't chase drivers or the pollers.
  - Maxwell (sm_52) lesson: the `draft-mtp` speculative-decode path CUDA-faults on
    the Titan X, and only on a real request (load/health pass first). Validate a
    new GPU with an actual streaming completion through the router, not just
    `llama-bench` — a bench without the draft/serving path gives a false pass.

## Alternatives considered
* **RMA / repair the P100** — out of warranty, used Volta-era datacenter card;
  HBM is not field-serviceable. Rejected.
* **Run headless on two V100s only** — would drop the `fast` P100 carve-out
  entirely and leave idx0 empty; the Titan X was on hand and keeps the slot warm
  for testing. Rejected in favour of the stopgap.
* **Buy a replacement immediately** — the user may choose another P100 or a third
  V100 later; the stopgap + preserved build + preserved MoE config make that a
  clean swap-back without blocking on a purchase now.

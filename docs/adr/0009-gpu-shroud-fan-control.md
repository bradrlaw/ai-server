# ADR-0009: GPU shroud fan control via nct6775 PWM driven by nvidia-smi temps

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

> **Later changes (noted 2026-10-15):** the P100 below died on 2026-07-27; idx0 is now a 12 GB
> GTX Titan X stopgap ([ADR-0017](0017-p100-hbm-ecc-death-titan-x-stopgap.md)). The
> controller and its idx0 200 W cap carried over unchanged (`scripts/gpu-fan-control.config.json`);
> both V100s stay at 175 W. **2026-10-16:** the shroud-fan headers are *voltage-mode*, not the
> "4-pin PWM" headers the Context says, and the daemon now has a fan stall alarm — see the
> [2026-10-16 addendum](#addendum-2026-10-16-stall-alarm-voltage-mode-headers-fan-prep-fixes).

## Context
The three datacenter GPUs (2x V100, 1x P100) are **passively cooled** — they have
no onboard fans and expose **no fan control** (nvidia-smi cannot set fan speed on
Tesla cards). The user added **shroud fans** on each card, wired into the
motherboard's **4-pin PWM fan headers** (MSI X99A XPOWER GAMING TITANIUM, MS-7A21,
Nuvoton NCT679x Super-I/O). Without active control the cards **thermal-throttle**
(observed via `nvidia-smi dmon`).

Constraints:
- The BIOS fan curves react to CPU/board temps, **not GPU temps**, so they ramp too
  late for GPU-bound inference loads.
- Copilot cannot run sudo; setup is delivered as scripts the user runs.

## Decision
Drive the shroud fans from **GPU temperature**:
1. Load the **`nct6775`** Super-I/O driver so the PWM headers appear under
   `/sys/class/hwmon` (X99 needs `acpi_enforce_resources=lax`) —
   `scripts/setup-fan-sensors.sh`.
2. Identify which `pwmN` channel each card's shroud fan is on —
   `scripts/identify-fan.sh`.
3. Run a small **root daemon** (`scripts/gpu-fan-control.py`, stdlib-only) that
   polls `nvidia-smi` per-GPU temps and sets each mapped PWM header from a
   per-zone temp→duty curve, installed as a **systemd service**
   (`gpu-fan-control.service`). Config in `gpu-fan-control.config.json`.

Cooling curve mirrors the user's proven Windows FanControl setup: **V100s** idle
39 °C → load 70 °C, **35→100 %** duty; **P100** idle 38 °C → load 70 °C,
**70→100 %** duty (its fan is 6k-rpm/quieter vs the V100s' 15k-rpm fans). Linear
between idle and load, 100 % at/above 70 °C.

**Fail-safe:** any daemon error or a failed `nvidia-smi` forces fans to **100%**;
on clean exit fans are handed back to BIOS auto; systemd `Restart=on-failure`.

## Consequences
- Positive: fans track the *actual* heat source (GPU), eliminating throttling;
  per-card zones; no external hardware needed.
- Negative: relies on `acpi_enforce_resources=lax` (ACPI + driver share the
  Super-I/O region — widely used but slightly outside vendor defaults). Requires a
  one-time reboot. PWM↔fan mapping is manual (identify step).
- Risk mitigation: fail-safe to 100% on any fault so a crash never removes airflow.

## Alternatives considered
- **BIOS fan curves** — rejected: react to CPU/board temp, not GPU; throttling seen.
- **`fancontrol` (lm-sensors)** — rejected as primary: it maps *mobo* temp sensors
  to PWM and cannot read `nvidia-smi` GPU temps, which is exactly what we need.
- **External USB fan controller** (e.g. Corsair Commander) — unnecessary; fans are
  already on the board's PWM headers.
- **Power-limit the GPUs** (`nvidia-smi -pl`) — a throttling *mitigation*, not a
  cooling solution; may still be layered on later for efficiency.

## Addendum (2026-07-01): mapping fix, HBM-temp control, integrated power caps

Three refinements after live validation:

1. **PWM↔GPU mapping was swapped.** The naive `identify-fan.sh` guess mislabeled the
   two V100 fans. A forced-fan idle cross-test (`verify-gpu-fan-mapping.sh`: one V100
   fan MAX / other FLOOR, observe which GPU cools) proved **pwm5→GPU1(bus03),
   pwm4→GPU2(bus04)**. Lesson: only the cross-test is conclusive; RPM correlating with
   a GPU's temp under the daemon is circular. Config corrected + zones relabeled.

2. **Control off HBM memory temp, not core.** The V100 HBM2 `temperature.memory` runs
   ~15-20 °C hotter than the core and is the throttle limiter (~85 °C). `gpu_temps()`
   now returns `max(core, mem)` per GPU and drives the fan off that (P100 has no mem
   sensor → core). Even at 100 % fan the 40 mm shroud can't beat a full memory-bound
   load — HBM plateaus at 85 °C and the card soft-throttles (safe, within HBM2 spec).

3. **Power caps are now applied by this service at boot** (promoting the "layered on
   later" alternative to Accepted). `power-cap-sweep.sh` characterized each card:
   **both V100s → 175 W** (holds ~83-84 °C vs 85 °C throttle at stock 250 W, ~91 %
   decode throughput retained), **P100 → 200 W** (never throttles; a longevity/noise
   trim at ~0 % throughput cost). Implemented as a top-level `power_limits` object in
   `gpu-fan-control.config.json`; `apply_power_limits()` runs `nvidia-smi -i N -pm 1
   -pl W` at startup. Rationale: fewer moving parts (one service owns thermal policy),
   and caps survive reboot without a separate unit. **Non-fatal by design** — a cap
   failure never stops the fan loop, since airflow is the safety-critical function.
   Motivation: longevity + noise until the cooling solution is upgraded.

## Addendum (2026-10-16): stall alarm, voltage-mode headers, fan-prep fixes

Prep for a cooling swap (a shroud with two 15k-rpm 40 mm fans on one V100, a blower shroud
on the other), from audit items A4–A8:

1. **Stall alarm, holding the fan at 100 % until it spins (A4).** The daemon never read a
   tach, so a dead or unplugged fan was silent. Each driven zone now watches a tach: by
   default `fanN_input` for `pwmN`, or `"tach": "fanM_input"`, or `null` when the fan has no
   tach. The default `stall_rpm` is 300.
   - **Detect:** 3 ticks (~12 s) below `stall_rpm` while the last duty was ≥ 25 % →
     `WARNING ... fan STALLED`, and the zone is held at **100 %**.
   - **Recover:** 3 ticks back above `stall_rpm` → back on the curve, with a "raise
     `min_duty`" hint in the log.
   - **Why hold at 100 %:** it doubles as a kick-start for a fan that can't start at a low
     header voltage (see 2).
   - **Alternatives rejected:**
     - *Alarm only:* the owner chose the hold.
     - *Latch at 100 % until a restart:* a transient, like a bumped cable, would leave the
       fan loud for good.
   - **Status page:** the daemon writes a per-tick snapshot to
     `/run/gpu-fan-control/state.json` (0644, atomic; the unit's `RuntimeDirectory=` removes
     it on stop). The status page uses it for a Fan column in the GPU table. A stall, a
     stopped or hung daemon, or a fail-safe also show on the summary line and turn the Open
     WebUI banner amber.
   - **A bad `tach` only disables the alarm** (airflow comes first);
     `install-fan-service.sh` rejects it up front.
   - **Sysfs writes now use `with open(...)`.** The old `open(path, "w").write(x)` reported
     write errors only at the garbage-collected close, where Python discards them. So a
     rejected pwm write went unnoticed, and `restore_auto()`'s mode-5 → mode-2 fallback
     could never trigger.

2. **The headers are voltage-mode, not PWM (A5).** This corrects the Context above and the
   audit. The board manual (p48) says:
   - Only **CPUFAN1** is PWM-mode.
   - SYSFAN1, PUMPFAN1, OPTFAN1 and OPTFAN2 are 4-pin *voltage* headers (pin 4 not
     connected); SYSFAN2 and SYSFAN3 are 3-pin voltage headers.

   So the daemon's duty sets a voltage. A 3-pin fan or a blower is speed-controlled there;
   a 4-pin PWM fan follows the voltage. The audit's advice to use a SATA/molex-powered PWM
   splitter would have left the 15k pair at 100 % permanently, since that hub powers the
   fans from 12 V and the PWM signal it would pass on doesn't exist. Instead use:
   - a passive Y-splitter (only one tach is read), or
   - one header per fan, with a second zone for the same GPU.

   MSI publishes no per-header current rating, so the runbook budgets the common 1 A.
   `pwmN_mode` is deliberately left alone. In kernel 6.8 `nct6775-core.c`, the nct6792 has a
   mode register for channel 1 only (`NCT6776_REG_PWM_MODE`/`_MASK`), so `pwm2`–`pwm5` always
   read 1 and reject 0, whatever the header really is. Flipping `pwm1`'s bit could mismatch
   the board circuit the BIOS configured. Details:
   [server-setup.md "Fan headers"](../server-setup.md#fan-headers-voltage-mode-splitters-current-2026-10-16).

3. **Side fan and `identify-fan.sh` (A6).** The 210 mm side fan's tach (`fan3`) reads 0 rpm;
   a physical check is now in the re-wiring checklist. `identify-fan.sh` had several fixes:
   - It now stops `gpu-fan-control` during its test and restarts it afterwards. Before, it
     fought the daemon over `pwm4`/`pwm5` and measured garbage for them.
   - Ctrl-C no longer resumes the loop.
   - Each channel is handed back right after it's measured, instead of being parked at 23 %.
   - It counts only fans that *speed up*, so the previous channel's spin-down isn't
     misattributed.
   - It says when a zone needs an explicit `tach`.

   `verify-gpu-fan-mapping.sh` reads each zone's `tach`.

4. **Docs (A7).** The runbook said "100 % by 80 °C"; the curve reaches 100 % at 70 °C. It
   now also says that only `power_limits` is re-read live; anything else needs a restart.
5. **Labels (A8).** Stale P100 labels are fixed in the daemon docstrings, `identify-fan.sh`
   and `gpu-dmon-load-capture.sh`. The capture script now names GPUs from `nvidia-smi` and
   handles any index set.
6. **Hardening from review.**
   - **A hung `nvidia-smi` can no longer freeze the loop.** Every call is bounded (15 s).
     A timed-out child is killed but not waited for, since a GPU that falls off the bus can
     wedge it inside the driver, and no new call starts while it is still stuck. The loop
     treats that as an `nvidia-smi` failure, so the fans go to 100 %. Before, a hang left
     the fans at their last duty, and the only sign was the status page's stale-state alert.
   - **The fail-safe reports its own failure.** If the 100 % write fails too, the zone
     error or fault says "100% write FAILED" instead of claiming the fan is at 100 %.
   - **Config checks.** The daemon ignores a `stall_rpm` that isn't an integer 0–20000
     (warning, default 300), as the installer does; `false` used to become 0 and turn the
     alarm off. The installer rejects two zones on one `pwm`; the daemon logs a warning.
   - **`verify-gpu-fan-mapping.sh`** exits before touching a fan if the daemon won't stop
     (it used to carry on and fight it), and also aborts if a tested GPU's HBM passes
     80 °C (`ABORT_HBM_C`).
   - **Left as is:** a daemon restart re-arms the alarm from scratch, so a dead fan is held
     at 100 % again after ~4 ticks. A unit that runs without publishing state shows
     "fans: unknown" on the status page but raises no banner alert: the daemon is still
     controlling the fans, and a hang after its first write already shows as stale.

Deploy (owner, sudo): `sudo scripts/install-fan-service.sh` (installs the unit with
`RuntimeDirectory=`, validates, restarts the daemon), then `sudo systemctl restart
server-status`.

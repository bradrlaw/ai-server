# ADR-0009: GPU shroud fan control via nct6775 PWM driven by nvidia-smi temps

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

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

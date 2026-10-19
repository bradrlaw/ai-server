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

3. **Side fan and `identify-fan.sh` (A6).** The 210 mm side fan (Cooler Master
   A23030-07CB-3MN-F1) spins, but `fan3` reads 0 rpm, and `identify-fan.sh` found no tach on
   `pwm3` on 2026-07-01 either. No spec sheet says whether the fan has a speed sensor; the
   owner will check the BIOS reading during the shroud swap. Until then it can only be checked
   by eye, so the re-wiring checklist says to look. `identify-fan.sh` had several fixes:
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

## Addendum (2026-10-17) — Dual-fan shroud rework after acoustic baseline test

**Physical change (owner, same day as the acoustic baseline, before swap).** The two
40x28mm 15k-rpm fans that were one-per-V100 are now both mounted in a single two-fan
shroud on V100 idx1 (bus03, top `PCI_E1` slot). The other V100 (idx2/bus04) now has a
single blower attached to the `OPTFAN1` header (same headers as before -- no splitter).
The 210mm side fan, previously on a splitter shared with the rear case fan (itself on an
external switch-based speed controller, not a motherboard header), moved onto `OPTFAN2`
directly.

**Re-mapped with identify-fan.sh + manual probing** (GPUs idle):
- `pwm4`+`pwm5` (both still 15k-class per `identify-fan.sh`'s classifier) now **both**
  drive V100 idx1 -- confirmed by the card's physical slot/bus match to the owner's
  description. `verify-gpu-fan-mapping.sh`'s automated cross-test was **inconclusive**
  for them (idle GPUs sit within 1 °C of ambient, too small a thermal signal to prove a
  delta automatically) but both have working tachs (`fan4_input`/`fan5_input`, ~6-15k rpm
  observed).
- `pwm1` (`OPTFAN1`), initially thought to drive the blower now cooling idx2 (an early
  100%-ramp test was "confirmed by ear") -- **retracted same day, see the 2026-10-17
  "pwm1/pwm3 do not control anything" addendum below.** That initial result was a false
  positive: it ran immediately after `verify-gpu-fan-mapping.sh` had just finished forcing
  `pwm4`/`pwm5` to extremes, and two later, confound-free tests (isolated 0%/100% with a
  hand at the exhaust, and a full randomized blind sweep of every channel) both showed
  **zero** response from `pwm1`.
- `pwm3` (`OPTFAN2`, the 210mm fan's new header) had **zero observable effect** at either
  0% or 100% duty -- the fan kept spinning at 0% (should have stopped if the header were
  truly voltage-controlled by this register) and showed no audible/tactile change at
  100%. Research into this board family (MSI X99A XPOWER GAMING TITANIUM) found
  user-documented cases where `OPTFAN1`/`OPTFAN2`/`PUMPFAN1` are BIOS/EC-managed and not
  reliably addressable via the generic `nct6775` Linux driver, even though BIOS Hardware
  Monitor can see and adjust them directly -- consistent with what we observed. No zone
  has ever driven `pwm3` (it was already BIOS-auto/undriven pre-move, just logged "check
  by eye"), so this is a documentation correction, not a regression: we no longer assert
  `pwm3` controls the 210mm fan, and it stays outside daemon control either way.
- `pwm2` (case/CPU fan) unaffected by any of this, unrelated zone.

**Config changes (`gpu-fan-control.config.json`):** split the single V100-idx1 zone into
two (`pwm5`+`pwm4`, identical curve, both targeting `gpus: [1]`, so they always move
together); added a new idx2 zone on `pwm1` (`tach: null`, curve copied from the 15k-fan
zones as an unvalidated starting point -- blower airflow/noise characteristics differ
from the axial fans, retune after a thermal comparison); dropped the `pwm`/`hwmon_name`
fields from the Titan X monitor-only zone, since `pwm1` is now actually driving idx2's
blower instead of just being handed back to BIOS auto unused.

**Next:** re-run `fan-noise-sweep.py`/`fan-noise-analyze.py` with the new mapping and
compare against the `before-swap` baseline (`docs/data/acoustic/`), plus watch idx2's
HBM temp under load for a few days since the blower zone's curve is unvalidated.

## Addendum (2026-10-17) — Cooling test result: dual-fan shroud underperforms

Ran `power-cap-sweep.py --gpus 1 2 --caps 175 --soak 300 --free --label
dual-fan-idx1_blower-idx2` (steady-state soak, 175 W cap -- the cap both V100s already
run at) to compare the new layout against the pre-rework baseline (2026-07-01 table
above: both V100s held 83-84 °C at 175 W, no throttle).

| | GPU1 -- dual-fan shroud (bus03, top `PCI_E1` slot) | GPU2 -- single blower (bus04) |
|---|---|---|
| Peak HBM (soak) | **85 °C** | **70 °C** |
| Peak core | 68 °C | 57 °C |
| Fan duty | 100% (maxed) | 100% (maxed) |
| Thermal throttle | **33% of samples** | none |
| Cool-down to target | **25.3 s** | 0.7 s |

Full data: `docs/data/thermal/power-cap-sweep-20261017-075854-dual-fan-idx1_blower-idx2.csv`.

**Result: the two-fan shroud is a regression, not an improvement.** GPU1 now
soft-throttles at the exact cap (175 W) it previously held at 83-84 °C with headroom to
spare, and takes 36x longer to cool back down than GPU2. The blower, meanwhile,
significantly *outperforms* the single 15k axial fan it replaced (70 °C vs. the old
83 °C -- ~13 °C of new headroom). Both fans are already at 100% duty in both cases, so
this isn't a fan-curve/tuning problem -- it's airflow.

**Leading hypothesis:** the two fans crammed into one shroud on GPU1 are starved for
intake or recirculating their own exhaust, rather than adding cooling capacity; the top
`PCI_E1` slot may also have less front-panel intake clearance than the slot GPU1's single
fan used to occupy (slot-position confound -- the tool's own usage note recommends
swapping shrouds between cards to rule this out: "swap the shrouds between the cards and
repeat (removes the slot-position confound)").

**Not yet resolved** -- `audit-cooling-test` stays open. Next step is the owner's call:
swap which physical card gets the dual-fan shroud vs. the blower (isolates slot position
from shroud design), or redesign the two-fan shroud's intake/exhaust path before
re-testing, or revert to one 15k fan per V100 (the previously-proven-good layout) and
keep the blower as a straight upgrade.

**Caveat on the GPU2/blower numbers above:** as of the next addendum, we've since
established that `pwm1` does **not** actually control the blower -- so GPU2's 70 °C
result reflects whatever the blower's BIOS/EC-driven speed happens to be, not anything
our daemon did. The measurement is real and still useful data, but "the blower
outperforms" should be read as "the blower (at its own fixed/uncontrolled speed)
outperforms," not "our tuned blower curve outperforms."

## Addendum (2026-10-17) — pwm1 and pwm3 do not control anything (confirmed)

The `pwm1`="blower" identification above was wrong. Two further, more rigorous tests
settled it:

1. **Isolated 0%/100% test**, with the 210mm fan physically unplugged to remove any
   confound and the owner's hand directly at the blower's exhaust (airflow is a more
   reliable check than sound for a quiet blower): true 0% and true 100% on `pwm1` produced
   **no detectable change** in airflow or sound.
2. **Blind, randomized full-channel sweep** (`scripts/sweep-fan-channels.sh`): all five
   `pwm1`-`pwm5` channels held at a safe ~40% floor, then each ramped to 100% in turn in a
   shuffled, un-announced order, asking the owner to describe what changed with no
   knowledge of which channel was being tested. Results:

   | channel | owner's response | identity |
   |---|---|---|
   | pwm1 | "nothing" | OPTFAN1 / blower -- **no response** |
   | pwm2 | "I think cpu fan" | case/CPU fan (pre-existing, unrelated) -- correct |
   | pwm3 | "nothing" | OPTFAN2 / 210mm fan -- **no response** |
   | pwm4 | "that sounds like one of the 15k fans" | V100 idx1 shroud fan -- correct |
   | pwm5 | "that sounds like the other 15k fan" | V100 idx1 shroud fan -- correct |

   Every channel that previously tach-confirmed or made audible sense (`pwm2`, `pwm4`,
   `pwm5`) was correctly identified blind; `pwm1` and `pwm3` were both silently inert, same
   as the isolated tests. This is about as conclusive as a non-invasive test gets.

**Conclusion:** on this board (MSI X99A XPOWER GAMING TITANIUM, nct6792 Super I/O),
`OPTFAN1` (blower, idx2/GPU2) and `OPTFAN2` (210mm fan) are **not controllable from Linux
at all** via the generic `nct6775` driver, even though BIOS Hardware Monitor can see and
adjust them directly -- this matches documented reports for this board family (official
MSI forum thread + general NCT6775-family discussion) that these headers are commonly
BIOS/EC-managed and not exposed as individually-addressable PWM channels to the OS.

**Config change:** the idx2/GPU2 "blower" zone is now `"monitor_only": true` (still lists
`pwm1` so the daemon hands it back to BIOS auto, mode 5, at startup instead of leaving it
in a stale manual state from testing -- but no curve, no duty writes, no stall alarm). We
do not control this fan's speed and cannot detect a stall; its 70 °C/no-throttle result in
the cooling test above is whatever the BIOS/EC already does with that header, independent
of anything in this config. Likewise `pwm3`/210mm was never driven by any zone and stays
that way.

**Open follow-up:** check BIOS Hardware Monitor's setting for `OPTFAN1` at the next
reboot (what mode/curve/sensor it's tied to) to understand what's actually keeping the
blower cool enough to be useful, and confirm there's no scenario where it could fail to
respond to a real GPU2 temperature spike with no software safety net to catch it.

## Addendum (2026-10-17, later same day) — gpu<->fan mapping was reversed (corrected)

**The original bus-id/slot-based mapping was wrong.** Everything above in the two prior
same-day addenda assumed idx1 (bus03, the "top `PCI_E1` slot") has the dual-fan shroud and
idx2 (bus04) has the blower, based on the owner's verbal description of card positions.
That assumption has now been **proven wrong by a direct causal test**: while the system
was under load, the owner unplugged the blower's power cable entirely. Result: **idx1**
(not idx2) started heating up, and the dual-fan-shroud zone (`pwm4`/`pwm5`, configured at
the time to track idx1's temp) spun up in response -- but couldn't cool it, because those
fans are not physically mounted on idx1's card. idx1 reached 85 °C before the blower was
reconnected.

**Corrected mapping (causally proven, not inferred from bus-id):**
- **idx1** -- cooled by the blower (OPTFAN1/`pwm1`), still unproven/uncontrolled from
  software (see the retraction addendum above).
- **idx2** -- cooled by the two-fan shroud (`pwm4`+`pwm5`).

This is the reverse of every zone assignment made in the two addenda above. The cards
were most likely reseated into different PCIe slots during the physical fan rework, so
the original bus-id-based index<->slot correlation no longer held -- a reminder that
**NVIDIA's PCI bus-id enumeration order does not reliably predict which physical card is
in which slot after any reseating**, and should never be trusted alone for a fan mapping;
only a direct causal test (temporarily disable one cooling source under load and see
which GPU's reported temp actually moves) is conclusive.

**This also retroactively explains the confusing 2026-10-17 cooling-test result** (the
"dual-fan shroud underperforms" addendum, two above): the card that hit 85 °C/33%
throttle during that test was controlled by a zone that was reading temps from the WRONG
physical GPU (driving `pwm4`/`pwm5` based on idx1's heat, while those fans actually sit on
idx2's card) -- so whichever card was actually hot had **no real fan responding to its own
temperature**, and whichever card got blasted with max airflow was responding to someone
else's heat, not a reflection of its own needs. There is **no evidence of an airflow/shroud
design problem** after all -- the "regression" was a software mapping bug, not a hardware
one. The slot-position/shroud-starvation hypothesis in that addendum is retracted.

**Config fixed:** all zone `"gpus"` fields corrected (dual-fan-shroud zones -> `[2]`,
blower zone -> `[1]`); zone names renamed from the bus-id-based `"V100 GPU1 (bus03) ..."`
/ `"V100 GPU2 (bus04) ..."` to `"V100 idx2 dual-fan shroud, ..."` / `"V100 idx1 blower
(uncontrolled)"` to avoid re-encoding an assumption about slot position into the name
itself. `docs/server-setup.md`'s monitoring one-liner updated to match.

**Next:** re-run `power-cap-sweep.py --gpus 1 2 --caps 175 --soak 300 --free` with the
corrected mapping to get a trustworthy A/B of the new physical layout (the prior result's
numbers are still real measurements, just mis-attributed -- re-running isn't strictly
required to get correct totals, but is required to attribute them to the right physical
card/fan combination). `audit-cooling-test` stays open pending that re-run and the
owner's decision on the still-separate, still-open question of whether `SYSFAN1` (being
tested as a replacement header for the blower, in place of `OPTFAN1`) is actually
controllable from Linux.

## Addendum (2026-10-17) — SYSFAN1 confirmed controllable, blower moved, final results

**Blower moved from OPTFAN1 to SYSFAN1** (required a power-off to re-seat the connector).
A second blind, randomized full-channel sweep (`scripts/sweep-fan-channels.sh`, kept in
the repo for reuse) confirmed `pwm3` (SYSFAN1) drives the blower ("pwm3 is the blower fan
for sure"), while `pwm1` (now-empty OPTFAN1) showed nothing -- consistent with the earlier
finding that OPTFAN1 itself is not controllable from Linux on this board.

**Min-duty-to-start wrinkle:** at the sweep's ~39% test floor, `fan3_input` read 0 and idx1
idled hot (57-58 °C core at 0% utilization, no felt vibration) -- looked at first like
another OPTFAN1-style dead end. Forcing 100% duty immediately dropped idx1 to 42 °C and
`fan3_input` jumped to ~4800 rpm, proving the tach and fan are both fine -- this fan simply
needs a higher duty to start spinning. The owner recalled BIOS Hardware Monitor warning
that <40% duty on this header "may lead to issues," which matches exactly. `min_duty`
raised to 45% (40% + 5% margin) in the idx1 blower zone.

**Also corrected:** `pwm3`'s earlier "zero response" result (logged in the previous
addendum as testing OPTFAN2/the 210mm fan) was a mislabeling -- that test ran before the
blower moved, when SYSFAN1 simply had nothing plugged into it. An empty header reads
"nothing" in a sweep/ramp test regardless of its own controllability, so that result was
never real evidence about OPTFAN2. **OPTFAN2's actual pwm channel remains
untested/unknown** -- not pursued further per owner's direction (see below).

**Final verification -- both V100 zones load-tested individually** (`power-cap-sweep.py
--gpus N --caps 175 --soak 180 --free`, post-fix):

| | idx1 -- blower (SYSFAN1/`pwm3`) | idx2 -- dual-fan shroud (`pwm4`+`pwm5`) |
|---|---|---|
| Peak HBM (soak) | 78 °C | 70 °C |
| Peak core | 60 °C | 58 °C |
| Fan duty / rpm | 100% / ~4880 rpm | 92-98% / ~14.7-15.3k rpm |
| Thermal throttle | 0% | 0% |
| Cool-down to target | 2.8 s | 2.2 s |

Both zones are healthy with real headroom below the ~85 °C throttle point, and both now
have a working stall alarm (previously impossible for the blower with no confirmed-working
tach). `audit-cooling-test` marked **done**.

**Owner's take and next physical step (not yet scheduled):** the blower is noisier than
expected for its cooling performance (78 °C vs. the dual-fan shroud's 70 °C, at a
comparable or louder sound level) -- the owner suspects the shroud geometry is the cause:
the current shroud routes the blower's intake through a 90°-ish curve so the fan sits
alongside the card rather than in-line at its end, which costs static pressure/airflow
efficiency versus a straight-through duct. The owner is going to look for a
straight-through shroud/blower combo and see if it physically fits the case before
deciding whether to replace it. No config change needed for this -- revisit the idx1
blower zone's curve/min_duty if a new blower is installed (repeat the
min-duty-to-start check; different fans can have different floors).

**210 mm side fan (OPTFAN2) -- deliberately not pursued further.** Owner is reverting it
to its original header/connector, which is already known to be uncontrollable from
software (same conclusion reached before this session's blower work, just via a different,
now-corrected test). No further investigation planned; this is an accepted, pre-existing
limitation (same posture as before the whole dual-fan-shroud rework).

## Addendum (2026-10-19) — straight-through shroud does not fit; replacement abandoned

Owner sourced and received a candidate straight-through-duct shroud (intended to improve
on the curved shroud's apparent static-pressure loss, see the 2026-10-17 addendum). On a
physical test-fit, **it did not clear the GPU's power connector or the power cable
routing** -- installing it fully would have required either forcing the shroud against
the connector/cable or bending the cable in a way that risked damaging the card (cracked
connector, strained/shorted cable, or PCB stress). Owner correctly stopped before forcing
it and did not install or test it.

**Outcome: this specific shroud is abandoned; no thermal test was run.** The idx1 blower
configuration (SYSFAN1/`pwm3`, `min_duty` 45%, curved shroud, 78 °C peak HBM under 175 W
soak, 0% throttle) from the 2026-10-17 addendum remains the final, deployed, working
state -- no regression, nothing to revert. If the owner looks for another straight-through
candidate in the future, add power-connector/cable clearance (not just fan-to-GPU-end
clearance) to the fit checklist before ordering -- this is the dimension that was missed
here. No config or code changes needed for this addendum.

## Addendum (2026-10-19) — 210mm side fan reinstalled; size corrected to 230mm, BIOS-driven at max

Owner reinstalled the side fan (model **A23030-07CB-3MN-F1 / DF2303012SELN**) on the
`OPTFAN2` header. **Size correction: this fan is 230x200mm, not 210mm** as logged
throughout this ADR and `server-setup.md`'s rewiring checklist -- the model number itself
was always right, only the diameter figure was wrong. Left the earlier "210mm" text
in place elsewhere in this ADR as a historical record of what was believed at the time;
this entry is the correction of record.

Speed is set directly in BIOS Hardware Monitor to max/100% (not via the Linux daemon --
consistent with the 2026-10-17 finding that `OPTFAN2` is not reliably addressable from
`nct6775`/Linux on this board). At that BIOS-set 100%, the fan spins at **~730 rpm** and
is not noticeable above the machine's other fans. 730 rpm at "100%" is plausible for a
large, low-speed 230mm case fan (slow but high air-volume by design) and is not evidence
of a stuck/throttled header.

No config or daemon changes: `pwm3`/`OPTFAN2`'s channel remains outside `gpu-fan-control`'s
scope (BIOS-managed), per the 2026-10-17 "deliberately not pursued further" decision. This
addendum is a documentation-only correction of the fan's size and a record of its current
BIOS-set operating point for future reference (e.g. if it ever needs rechecking).

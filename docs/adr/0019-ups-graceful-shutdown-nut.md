# ADR-0019: UPS monitoring & graceful shutdown (CyberPower CP1500 via NUT)

- **Status:** Accepted
- **Date:** 2026-08-03
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Update 2026-08-08 — UPS hardware swapped (CP1500AVRLCD → CP1500PFCLCD)
The original **CP1500AVRLCD** (and a warranty replacement of the same model)
turned out to be **electrically incompatible** with the server's active-PFC PSU.
Both units use a **stepped / simulated sine wave** on battery, which the PFC
front-end rejects: on mains loss the server would not ride through, and it could
not **cold-start** while the UPS was running on battery — the fans would try to
spin, then the PSU folded back and shut off (PFC-reject + GPU inrush). Load was
not the issue (~200 W idle); the **output waveform shape** was.

Replaced with a **CyberPower CP1500PFCLCD** (PFC **Sinewave**, 1500 VA / **1000 W**;
reports `device.model = CP1500PFCLCDa`). It produces a true sine wave the
active-PFC PSU accepts: the server now rides through mains loss **and cold-starts
on battery** (verified 2026-08-08). The new unit enumerates with the **same USB
VID:PID `0764:0601`**, so NUT's `usbhid-ups` bound with **no config change** —
`scripts/ups-setup.sh` and everything below still apply as-is. First live reading:
`OL CHRG`, battery 96 %, ~41 min runtime.

**Takeaway for any future UPS on this box: it must be a pure/true sine-wave unit
(active-PFC PSU requirement); avoid "simulated/stepped sine" AVR models.**

## Context
A CyberPower **CP1500AVRLCD** UPS (1500 VA / ~900 W, USB HID Power Device,
VID:PID `0764:0601`, enumerates as `CPS CP1500AVRLCD3` on hidraw) was connected
to the headless AI server over USB so the machine can power down cleanly during
an extended outage instead of losing power mid-inference or mid-write to the
encrypted volume.

Constraints:
- Headless box; no vendor GUI. The kernel already exposes the UPS as a standard
  HID Power Device, so NUT's native `usbhid-ups` driver works without
  CyberPower's proprietary PowerPanel/`pwrstatd` daemon.
- The agent cannot `sudo`; privileged install/config is delivered as a script
  the owner runs (`scripts/ups-setup.sh`).
- Under GPU load the CP1500's runtime is short, and a clean shutdown (stop
  Docker app tier, unload GPUs, flush the LUKS volume) takes time — so the
  shutdown trigger matters.

## Decision
Use **NUT (Network UPS Tools)** in **standalone** mode with the `usbhid-ups`
driver, configured by `scripts/ups-setup.sh`. Trigger a graceful
`shutdown -h` on the UPS **LOW-BATTERY** signal (`OB LB`): ride through short
blips on battery, and power off only when the battery is nearly empty
(maximises uptime). `upsd` listens on loopback only (`127.0.0.1:3493`).

## Consequences
- Positive: brief outages are absorbed silently; extended outages end in a
  clean, unattended shutdown. Native driver, no vendor daemon, no cloud.
- Positive: config is reproducible and version-controlled via the script;
  `/etc/nut/*.conf` back up once (`*.orig-preNUTsetup`) and the generated
  monitor password is preserved across re-runs.
- Negative / trade-offs: LOW-BATTERY trigger leaves little reserve — if a clean
  shutdown ever exceeds remaining runtime the box could still drop hard. Watch
  the first real/drill event and, if too tight, switch to a timed or
  charge-threshold trigger (via `upssched`).
- Follow-ups / things to watch:
  - Run `sudo /srv/ai/scripts/ups-setup.sh`, then verify with `upsc cyberpower`.
  - Drill once with `upsmon -c fsd` (forces a shutdown — schedule it).
  - Consider surfacing `ups.status` / `battery.charge` / `battery.runtime` on
    the server-status dashboard.
  - Unrelated but noted the same boot: a flaky `usb 3-11` device on the Intel
    xHCI (`00:14.0`) fails enumeration and adds ~70 s to boot (recurring, not
    the UPS). Track down / unplug the offending internal USB device/header.

## Alternatives considered
- **CyberPower PowerPanel (`pwrstatd`)** — vendor .deb, proprietary, less
  transparent, and unnecessary since NUT's `usbhid-ups` already speaks to this
  UPS. Rejected.
- **`apcupsd`** — APC-oriented; CyberPower support is second-class vs NUT's HID
  driver. Rejected.
- **Timed / charge-threshold shutdown** — predictable and leaves reserve, but
  cuts uptime on every outage. Kept as the documented fallback if the
  low-battery margin proves too tight.

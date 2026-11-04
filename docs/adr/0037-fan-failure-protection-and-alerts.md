# ADR-0037: Fan-failure protection and alerts

- Status: Accepted
- Date: 2026-11-04

## Context
After a hardware reshuffle the idx1 blower was left unplugged. The daemon detected the
stall (0 rpm at 100% duty) but only logged it and held 100%, which does nothing for a
fan that is not connected. idx1 reached 84 °C idle before the owner noticed.

## Decision
`gpu-fan-control.py` gains a `protect` guard (config key `protect`):
- **Stall:** the stalled zone's GPUs are capped to `stall_cap_watts` (100 W) at once, and
  restored when the fan recovers. Applied through the same `reconcile_power_limits`, so
  the 30 s reconcile does not undo it.
- **Heat:** an urgent alert at `thermal_warn_c` (85); `systemctl poweroff` after
  `thermal_shutdown_ticks` ticks at `thermal_shutdown_c` (88, 0 = off). Fires once.
- **Signals:** `wall`, ntfy.sh push (urgent), `/etc/profile.d/gpu-fan-alert.sh` login
  banner (reads `state.json`), `protect` block in `state.json`, and a Fans section in
  `verify-boot-migration.sh`.
- **ntfy:** the topic (acts as the password) is read from `protect.notify.topic_file`
  (`~/.config/ntfy/topic`, mode 600, outside the repo and the SMB share). Sends are
  best-effort, threaded with a 10 s timeout, rate-limited to one per `repeat_sec` per key.
  A guard failure never stops fan control.

## Consequences
- Needs the machine to be up to send; a dead machine is not detected (an external
  heartbeat such as healthchecks.io would cover that; not done).
- Public ntfy.sh is best-effort and unauthenticated; anyone with the topic can read alerts.
- `protect` edits need a daemon restart (`sudo scripts/install-fan-service.sh`).

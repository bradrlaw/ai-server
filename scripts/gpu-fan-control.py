#!/usr/bin/env python3
"""GPU-temp-driven fan control for the headless AI server.

Reads each GPU's temperature via nvidia-smi and drives the motherboard
Nuvoton (nct6775 driver) fan headers that the GPU shroud fans are wired to.

The passively-cooled Tesla V100s have NO onboard fan control, so their
user-added shroud fans MUST be driven externally -- this daemon does it.

Header types: on this board (MSI X99A XPOWER GAMING TITANIUM, manual p48) only
CPUFAN1 is a PWM-mode header; every other fan header is VOLTAGE-mode, so the duty
we write becomes the header's output voltage (a 3-pin fan or blower is speed-
controlled; a 4-pin PWM fan's pin 4 is unconnected, so its speed follows the
voltage). We never write pwmN_mode: the BIOS sets it to match the board's wiring,
and on the nct6792 the kernel can only change it for pwm1 anyway (pwm2-5 always
read 1 = "PWM", whatever the header really is).

Stall alarm: each driven zone watches its fan's tach. A fan that reads below
stall_rpm while we ask for a real duty is held at 100% (which also kick-starts a
fan that can't start at a low voltage) until it spins again -- see check_stall().
Live state for the status page: /run/gpu-fan-control/state.json (STATE_FILE).

Config: /srv/ai/scripts/gpu-fan-control.config.json  (see zones below). Only
power_limits is re-read live; any other edit needs a restart of this service.
Run as root (writes /sys/class/hwmon/.../pwmN). Install via the systemd unit.

Fail-safe: on ANY error or on exit, fans are forced to 100% (manual) or handed
back to the BIOS, so a crash never leaves the cards without airflow. A hung
nvidia-smi counts as an error (NVSMI_TIMEOUT), so it can't freeze the loop.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

CONFIG = os.environ.get("FAN_CONFIG", "/srv/ai/scripts/gpu-fan-control.config.json")
# Per-tick snapshot for the status page (world-readable; the unit's
# RuntimeDirectory removes it when the daemon stops, so "missing" = not running).
STATE_FILE = os.environ.get("FAN_STATE", "/run/gpu-fan-control/state.json")

# Stall alarm (per driven zone; see Zone.check_stall). A zone's fan is STALLED
# after STALL_TICKS consecutive ticks below its stall_rpm while the duty we last
# wrote was >= STALL_MIN_DUTY (below that a fan on a voltage-mode header may stop
# legitimately). It's then held at 100% until the tach reads >= stall_rpm for
# STALL_TICKS ticks in a row, after which it follows the curve again.
DEFAULT_STALL_RPM = 300
STALL_TICKS = 3
STALL_MIN_DUTY = 25

# nvidia-smi normally answers in well under a second. A GPU that falls off the
# bus can wedge it -- sometimes unkillably, inside the driver -- and waiting on
# it would freeze the fan loop at its last duty. So every call is bounded, and
# a timed-out child is killed but never waited for (see nvsmi()).
NVSMI_TIMEOUT = 15
_stuck = []     # killed nvidia-smi children that haven't exited yet


def log(msg):
    print(f"[gpu-fan] {msg}", flush=True)


def find_hwmon(name):
    base = "/sys/class/hwmon"
    for h in sorted(os.listdir(base)):
        p = os.path.join(base, h)
        try:
            if open(os.path.join(p, "name")).read().strip() == name:
                return p
        except OSError:
            pass
    return None


def nvsmi(args, stderr=None):
    """Run nvidia-smi with args and return its stdout.

    Raises CalledProcessError on a non-zero exit and RuntimeError after
    NVSMI_TIMEOUT s -- or at once while an earlier timed-out call is still
    stuck, so a wedged driver can't pile up a new hung process every tick
    (the main loop just stays in its 100% fail-safe until it recovers).
    """
    for p in _stuck[:]:
        if p.poll() is not None:            # finally exited: reaped, close its pipes
            _stuck.remove(p)
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
    if _stuck:
        raise RuntimeError(f"still stuck in an earlier call, pid {_stuck[0].pid}")
    p = subprocess.Popen(["nvidia-smi", *args], stdout=subprocess.PIPE, stderr=stderr,
                         text=True)
    try:
        out, err = p.communicate(timeout=NVSMI_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()                            # not waited for: see NVSMI_TIMEOUT
        _stuck.append(p)
        raise RuntimeError(f"timed out after {NVSMI_TIMEOUT}s") from None
    if p.returncode:
        raise subprocess.CalledProcessError(p.returncode, p.args, out, err)
    return out


def gpu_temps():
    """Return {gpu_index: {'core','mem','eff'}} from nvidia-smi.

    On the Tesla V100 the HBM2 memory (temperature.memory) runs ~15-20 C hotter
    than the GPU core and throttles at ~85 C -- so the fan MUST be driven off the
    memory temperature, not the core. We control on eff = max(core, mem). A card
    that reports temperature.memory as N/A (the Titan X; the P100 that idx0 had
    until 2026-07) falls back to core only.

    We also read fan.speed: the passive datacenter cards (the V100s) report N/A
    (they are cooled only by the chassis shroud fans this daemon drives), but a
    consumer card that self-cools with its own onboard fan (e.g. the GTX Titan X
    stopgap in idx0) reports its fan duty -- we surface that in the log so its
    self-managed cooling is visible.
    """
    out = nvsmi(["--query-gpu=index,temperature.gpu,temperature.memory,fan.speed",
                 "--format=csv,noheader,nounits"])
    temps = {}
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        idx, core = int(parts[0]), int(parts[1])
        mem = None
        if len(parts) > 2:
            try:
                mem = int(parts[2])
            except ValueError:      # "N/A" / "[Not Supported]"
                mem = None
        fan = None
        if len(parts) > 3:
            try:
                fan = int(parts[3])
            except ValueError:      # passive cards report "[N/A]"
                fan = None
        eff = max(core, mem) if mem is not None else core
        temps[idx] = {"core": core, "mem": mem, "eff": eff, "fan": fan}
    return temps


def query_present_gpus():
    """Return the set of GPU indices nvidia-smi can currently see (empty on error)."""
    try:
        out = nvsmi(["--query-gpu=index", "--format=csv,noheader,nounits"],
                    stderr=subprocess.DEVNULL)
        return {int(x.strip()) for x in out.strip().splitlines() if x.strip() != ""}
    except Exception:
        return set()


def current_power_limits():
    """Return {gpu_index: enforced_power_limit_W} for GPUs nvidia-smi can see."""
    limits = {}
    try:
        out = nvsmi(["--query-gpu=index,power.limit", "--format=csv,noheader,nounits"],
                    stderr=subprocess.DEVNULL)
        for line in out.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                limits[int(parts[0])] = int(round(float(parts[1])))
            except ValueError:
                pass
    except Exception:
        pass
    return limits


def wait_for_gpus(expected, timeout_sec):
    """Block until nvidia-smi reports >= expected GPUs, or timeout (bounded).

    Guards against the boot-time race where the daemon starts before the driver
    has enumerated every card (partial cap application). Bounded so a genuinely
    dead/absent card can never hang the fan daemon -- we proceed with whatever is
    present once the timeout elapses (fans are safety-critical and self-heal).
    """
    if not expected or expected <= 0:
        return
    deadline = time.time() + max(0, timeout_sec)
    while True:
        present = query_present_gpus()
        if len(present) >= expected:
            log(f"all {expected} GPU(s) present: {sorted(present)}")
            return
        if time.time() >= deadline:
            log(f"WARNING: only {len(present)} of {expected} GPU(s) present "
                f"after {timeout_sec}s ({sorted(present)}); proceeding -- "
                f"power caps will be applied to any card as it reappears")
            return
        time.sleep(2)


def read_power_limits(fallback=None):
    """Re-read just `power_limits` from the config file.

    Called every reconcile pass so the config is the LIVE source of truth: an
    external editor (e.g. the status dashboard's per-GPU power dropdown) can change
    a cap by rewriting the file atomically, and this daemon picks it up on the next
    pass and applies it via `nvidia-smi -pl` — a live adjustment that does NOT reset
    the GPU or interrupt running work. On any read/parse error we keep the last
    known-good limits so a mid-write or transient failure never drops the caps.
    """
    try:
        with open(CONFIG) as f:
            return json.load(f).get("power_limits") or fallback
    except Exception as e:  # noqa: BLE001 - never let a bad read stop the daemon
        log(f"power_limits re-read failed ({e}); keeping last-known values")
        return fallback


def reconcile_power_limits(power_limits, quiet=False):
    """Idempotently enforce per-GPU power caps (W) + persistence mode.

    power_limits: {gpu_index(str|int): watts}. For longevity/thermal reasons the
    passively-cooled Teslas are capped below their default board limit (V100 HBM2
    pegs 85 C at stock 250 W; 175 W keeps it ~83-84 C at ~91% throughput); the
    idx0 card gets its own cap (the Titan X stopgap self-trims to ~200 W at its
    83 C target anyway). Persistence mode keeps the cap sticky and the driver
    resident on this headless box.

    Called at startup AND periodically from the main loop, so a GPU that was
    missing at boot (or fell off the bus and returned after a PCIe re-probe) gets
    its cap applied automatically -- no manual intervention. Only acts on drift:
    GPUs already at their target are skipped (idempotent, quiet).

    Non-fatal: a failure here must NEVER stop the fan daemon -- airflow is the
    safety-critical function, power capping is only an optimization.
    """
    if not power_limits:
        return
    current = current_power_limits()
    for idx, watts in sorted(power_limits.items(), key=lambda kv: int(kv[0])):
        idx = int(idx)
        try:
            watts = int(round(float(watts)))
        except (TypeError, ValueError):
            log(f"power cap for GPU{idx}: invalid value {watts!r}, skipping")
            continue
        if idx not in current:
            continue  # GPU absent right now; retry on a later reconcile pass
        if current[idx] == watts:
            continue  # already at target -- nothing to do
        try:
            nvsmi(["-i", str(idx), "-pm", "1"], stderr=subprocess.PIPE)
            nvsmi(["-i", str(idx), "-pl", str(watts)], stderr=subprocess.PIPE)
            log(f"power cap GPU{idx} {current[idx]}W -> {watts}W (persistence on)")
        except subprocess.CalledProcessError as e:
            log(f"power cap GPU{idx} -> {watts}W FAILED (non-fatal): "
                f"{(e.stderr or e.stdout or '').strip()}")
        except Exception as e:
            log(f"power cap GPU{idx} -> {watts}W FAILED (non-fatal): {e}")


def interp(curve, temp):
    """Linear interpolate duty% from a sorted [[temp,duty],...] curve."""
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= temp <= t1:
            frac = (temp - t0) / (t1 - t0)
            return d0 + frac * (d1 - d0)
    return curve[-1][1]


class Zone:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.gpus = cfg["gpus"]
        self.hysteresis = cfg.get("hysteresis_c", 3)
        self.min_duty = cfg.get("min_duty", 30)
        # A monitor-only zone drives NO chassis PWM -- it exists purely to log a
        # GPU's temp (and its own onboard fan) when the card cools itself (e.g. a
        # consumer GTX Titan X, whose built-in fan replaced the passive P100's
        # chassis pump fan). Any pwm listed is only used to hand that header back
        # to BIOS auto at startup so we stop forcing an unused fan.
        self.monitor_only = cfg.get("monitor_only", False)
        self.curve = sorted(cfg.get("curve", []))
        self.pwm = None
        self.enable = None
        hw = None
        pwm_name = cfg.get("pwm")
        if pwm_name and "REPLACE_ME" not in pwm_name:
            hw = find_hwmon(cfg.get("hwmon_name", "nct6775"))
            if hw is None:
                raise RuntimeError(f"hwmon '{cfg.get('hwmon_name')}' not found")
            pwm_path = os.path.join(hw, pwm_name)
            if not os.path.exists(pwm_path):
                raise RuntimeError(
                    f"zone '{self.name}': pwm channel '{pwm_name}' not found "
                    f"({pwm_path}). Run identify-fan.sh and edit the config.")
            self.pwm = pwm_path
            self.enable = pwm_path + "_enable"
        elif not self.monitor_only:
            raise RuntimeError(
                f"zone '{self.name}': no pwm channel set. Run identify-fan.sh "
                f"and edit the config (or mark the zone monitor_only).")
        self._last_temp = None
        self.temp = None            # last {"core","mem","eff"} (for STATE_FILE)
        self.gpufan = None
        self.error = None           # last update error, if the zone is in fail-safe
        # Stall alarm (driven zones with a tach only).
        self.tach = None
        self.stall_rpm = DEFAULT_STALL_RPM
        self.duty = None            # duty % we last wrote (None before our first write)
        self.rpm = None             # last tach reading
        self.stalled = False
        self.stalled_since = None
        self.stalls = 0             # stall events since the daemon started
        self._low = 0               # consecutive ticks below stall_rpm
        self._ok = 0                # consecutive ticks back above it (while stalled)
        self._tach_err = False
        if self.pwm is not None and not self.monitor_only:
            self._setup_tach(cfg, hw, pwm_name)

    def _setup_tach(self, cfg, hw, pwm_name):
        """Pick the tach input the stall alarm watches.

        "tach" absent -> fanN_input for pwmN (same header on the nct6775);
        "tach": null  -> no alarm (fan without a tach wire, or the leg of a
                         Y-splitter whose tach isn't passed through);
        "tach": "fanM_input" -> that input of the same hwmon chip.
        Never fatal: a bad tach only turns the alarm off (airflow comes first);
        install-fan-service.sh rejects a misconfigured one up front.
        """
        sr = cfg.get("stall_rpm", DEFAULT_STALL_RPM)
        if isinstance(sr, bool) or not isinstance(sr, int) or not 0 <= sr <= 20000:
            log(f"WARNING {self.name}: bad stall_rpm {sr!r} (want an integer 0-20000); "
                f"using {DEFAULT_STALL_RPM}")
        else:
            self.stall_rpm = sr
        if "tach" in cfg:
            tach = cfg["tach"]
            if tach is None:
                return
        else:
            tach = f"fan{pwm_name[3:]}_input"       # pwm5 -> fan5_input
        if not isinstance(tach, str) or not re.fullmatch(r"fan\d+_input", tach):
            log(f"WARNING {self.name}: tach must be \"fanN_input\" or null -> stall alarm OFF")
            return
        path = os.path.join(hw, tach)
        if not os.path.exists(path):
            log(f"WARNING {self.name}: tach {path} not found -> stall alarm OFF "
                f"(set \"tach\" in the config, or null if the fan has none)")
            return
        self.tach = path

    def describe(self):
        if self.monitor_only:
            return "monitor only (card cools itself)" + (
                f"; {os.path.basename(self.pwm)} handed to BIOS auto" if self.pwm else "")
        s = f"{os.path.basename(self.pwm)}, min {self.min_duty}%"
        if self.tach is None:
            return s + ", stall alarm off (no tach)"
        if self.stall_rpm <= 0:
            return s + f", {os.path.basename(self.tach)} shown, stall alarm off (stall_rpm 0)"
        return (s + f", stall alarm if {os.path.basename(self.tach)} < {self.stall_rpm} rpm "
                f"at >= {STALL_MIN_DUTY}%")

    def set_enable(self, mode):
        if self.enable is None:
            return True
        try:
            with open(self.enable, "w") as f:
                f.write(str(mode))
            return True
        except OSError as e:
            log(f"{self.name}: cannot set {self.enable}={mode}: {e}")
            return False

    def write_duty(self, duty_pct):
        duty_pct = max(self.min_duty, min(100, duty_pct))
        raw = int(round(duty_pct / 100 * 255))
        # `with` so a rejected sysfs write raises here (it's only reported at close).
        with open(self.pwm, "w") as f:
            f.write(str(raw))
        self.duty = duty_pct
        return duty_pct

    def read_rpm(self):
        if self.tach is None:
            return None
        try:
            with open(self.tach) as f:
                rpm = int(f.read().strip())
        except (OSError, ValueError) as e:
            if not self._tach_err:
                log(f"WARNING {self.name}: cannot read {self.tach} ({e})")
                self._tach_err = True
            return None
        self._tach_err = False
        return rpm

    def check_stall(self):
        """Update self.stalled from the tach. Runs each tick BEFORE the new duty
        is written, so the reading reflects the duty written on the last tick.

        Holding 100% doubles as a kick-start: on a voltage-mode header a fan may
        not START at a low duty's voltage even though it keeps running there once
        spinning. Not latched: the zone returns to its curve once the fan spins.
        """
        rpm = self.rpm = self.read_rpm()
        if rpm is None or self.duty is None:
            return  # no tach / unreadable, or our first tick (fan hasn't seen our duty)
        if not self.stalled:
            if rpm < self.stall_rpm and self.duty >= STALL_MIN_DUTY:
                self._low += 1
                if self._low >= STALL_TICKS:
                    self.stalled, self.stalled_since, self._ok = True, time.time(), 0
                    self.stalls += 1
                    log(f"WARNING {self.name}: fan STALLED -- {rpm} rpm < {self.stall_rpm} "
                        f"at {self.duty:.0f}% for {STALL_TICKS} ticks; holding 100% "
                        f"until it spins")
            else:
                self._low = 0
        elif rpm >= self.stall_rpm:
            self._ok += 1
            if self._ok >= STALL_TICKS:
                held = time.time() - self.stalled_since
                self.stalled, self.stalled_since, self._low = False, None, 0
                log(f"{self.name}: fan stall cleared ({rpm} rpm after {held:.0f}s at "
                    f"100%) -> back on the curve. If it keeps stalling, the fan may not "
                    f"start at min_duty {self.min_duty}% on this voltage header: raise it")
        else:
            self._ok = 0

    def update(self, temps):
        # eff = max(core, memory) across this zone's GPUs; memory is the V100
        # throttle limiter and runs much hotter than the core under load.
        vals = [temps.get(i, {"core": 0, "mem": None, "eff": 0, "fan": None}) for i in self.gpus]
        t = max((v["eff"] for v in vals), default=0)
        core = max((v["core"] for v in vals), default=0)
        mem = max((v["mem"] for v in vals if v["mem"] is not None), default=None)
        gpufan = next((v["fan"] for v in vals if v.get("fan") is not None), None)
        self.temp = {"core": core, "mem": mem, "eff": t}
        self.gpufan = gpufan
        label = f"{core}C" if mem is None else f"c{core}/m{mem}C"
        # A self-cooled card (consumer GPU with its own fan) reports its fan duty;
        # passive V100s report N/A. Surface it so the card's own cooling shows.
        if gpufan is not None:
            label += f" gpufan{gpufan}%"
        if self.monitor_only:
            # No chassis fan to drive; the card manages its own cooling.
            return label, None
        # hysteresis: only react if temp moved enough
        if self._last_temp is not None and abs(t - self._last_temp) < self.hysteresis:
            t = self._last_temp
        else:
            self._last_temp = t
        self.check_stall()
        duty = self.write_duty(100 if self.stalled else interp(self.curve, t))
        return label, duty

    def full_speed(self):
        """Manual 100%. Returns False if a sysfs write failed (fan state unknown)."""
        if self.monitor_only or self.pwm is None:
            return True
        ok = self.set_enable(1)
        try:
            self.write_duty(100)
        except OSError as e:
            log(f"{self.name}: cannot write 100% to {self.pwm}: {e}")
            return False
        return ok

    def restore_auto(self):
        if self.enable is None:
            return
        # 5 = nct6775 "smart fan"/BIOS auto; fall back to 2, then full speed.
        for mode in (5, 2):
            try:
                with open(self.enable, "w") as f:
                    f.write(str(mode))
                return
            except OSError:
                continue
        self.full_speed()

    def snapshot(self):
        """This zone's live state for STATE_FILE (read by the status page)."""
        base = os.path.basename
        return {
            "name": self.name,
            "gpus": list(self.gpus),
            "pwm": base(self.pwm) if self.pwm else None,
            "monitor_only": bool(self.monitor_only),
            "temp": self.temp,
            "gpufan": self.gpufan,
            "duty": None if self.monitor_only or self.duty is None else round(self.duty),
            "tach": base(self.tach) if self.tach else None,
            "rpm": self.rpm,
            "stall_rpm": self.stall_rpm if self.tach else None,
            "stalled": self.stalled,
            "stalled_since": self.stalled_since,
            "stalls": self.stalls,
            "error": self.error,
        }


_state_err = {"last": None}


def write_state(zones, interval, fault=None):
    """Publish the per-tick snapshot atomically (0644) for the status page.
    Best-effort: a failure here never touches fan control (logged once per
    distinct error)."""
    tmp = STATE_FILE + ".tmp"
    try:
        state = {"ts": round(time.time(), 1), "pid": os.getpid(), "interval": interval,
                 "fault": fault, "zones": [z.snapshot() for z in zones]}
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATE_FILE)
        _state_err["last"] = None
    except Exception as e:  # noqa: BLE001 - never let the state file stop the daemon
        if str(e) != _state_err["last"]:
            log(f"state file {STATE_FILE} not written (non-fatal): {e}")
            _state_err["last"] = str(e)


def remove_state():
    for p in (STATE_FILE, STATE_FILE + ".tmp"):
        try:
            os.unlink(p)
        except OSError:
            pass


def main():
    with open(CONFIG) as f:
        cfg = json.load(f)
    interval = cfg.get("interval_sec", 4)
    expected_gpus = cfg.get("expected_gpu_count", 0)
    startup_wait = cfg.get("gpu_wait_timeout_sec", 90)
    power_recheck = cfg.get("power_recheck_sec", 30)
    power_limits = cfg.get("power_limits")

    # Guard the boot race: wait (bounded) for the driver to enumerate every card
    # before capping, so no GPU is silently left at its default limit.
    wait_for_gpus(expected_gpus, startup_wait)

    zones = [Zone(z) for z in cfg["zones"]]
    owner = {}
    for z in zones:
        if z.pwm in owner:
            log(f"WARNING: zones '{owner[z.pwm]}' and '{z.name}' both drive {z.pwm} -- "
                f"fix the config (install-fan-service.sh rejects this)")
        elif z.pwm:
            owner[z.pwm] = z.name
    reconcile_power_limits(power_limits)
    last_power_check = time.time()
    for z in zones:
        if z.monitor_only:
            z.restore_auto()  # hand any listed header back to BIOS; we only log
        else:
            z.set_enable(1)  # manual PWM
        log(f"zone '{z.name}': {z.describe()}")
    log(f"controlling {len(zones)} zone(s), interval {interval}s, "
        f"power recheck {power_recheck}s; state -> {STATE_FILE}")

    stop = {"flag": False}

    def handle(signum, _frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    def panic_full_speed():
        """Every driven zone to 100%; returns the zones whose 100% write failed."""
        return [z.name for z in zones if not z.full_speed()]

    try:
        while not stop["flag"]:
            # Periodically re-enforce power caps so a GPU that was missing at
            # boot or fell off the bus and returned gets capped automatically.
            # Also re-read the config each pass so an external edit to power_limits
            # (e.g. the status dashboard's per-GPU power dropdown) is applied live —
            # nvidia-smi -pl adjusts the cap without interrupting running jobs.
            if time.time() - last_power_check >= power_recheck:
                power_limits = read_power_limits(fallback=power_limits)
                reconcile_power_limits(power_limits)
                last_power_check = time.time()
            try:
                temps = gpu_temps()
            except Exception as e:
                log(f"nvidia-smi failed ({e}) -> fans to 100%")
                for z in zones:
                    try:
                        z.check_stall()   # a dead fan still matters at 100%
                    except Exception:     # noqa: BLE001 - panic path must not fail
                        pass
                failed = panic_full_speed()
                fault = f"nvidia-smi failed ({e}); fans at 100%"
                if failed:
                    fault += f" except {', '.join(failed)} (100% write FAILED)"
                write_state(zones, interval, fault=fault)
                time.sleep(interval)
                continue
            status = []
            for z in zones:
                try:
                    label, duty = z.update(temps)
                    z.error = None
                    if duty is None:
                        status.append(f"{z.name}:{label}[monitor]")
                    else:
                        s = f"{z.name}:{label}->{duty:.0f}%"
                        if z.rpm is not None:
                            s += f"@{z.rpm}rpm"
                        status.append(s + (" STALL" if z.stalled else ""))
                except Exception as e:
                    held = "fan at 100%" if z.full_speed() else "100% write FAILED too"
                    log(f"{z.name} update error ({e}) -> {held}")
                    z.error = f"update error ({e}); {held}"
            log(" ".join(status))
            write_state(zones, interval)
            time.sleep(interval)
    except Exception as e:
        log(f"FATAL {e} -> fans to 100%")
        panic_full_speed()
        raise
    finally:
        # graceful shutdown: hand fans back to the BIOS
        for z in zones:
            z.restore_auto()
        remove_state()
        log("exited; fans returned to automatic control")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("must run as root")
    main()

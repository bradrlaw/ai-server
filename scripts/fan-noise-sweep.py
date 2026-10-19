#!/usr/bin/env python3
"""Fixed-duty fan sweep for noise measurements.

With the GPUs idle, steps the GPU shroud fans (the driven zones of
gpu-fan-control.config.json) through fixed duties and logs when each step ran,
so ONE continuous microphone recording can be cut into a segment per fan and
duty by scripts/fan-noise-analyze.py and compared across cooling setups.

Schedule (default --duties 35,50,75,100; two fans = 11 steps x 60 s = 11 min):
  lead-in    all fans at the hold duty (default: the lowest of --duties)
  each fan   alone at each other duty, the rest at the hold duty
  all fans   together at each other duty
  lead-out   all fans back at the hold duty (lead-out vs lead-in = drift check)
Each step waits --settle s for the fans to reach speed, then holds --measure s:
the part the analysis uses.

Run it with the GPUs idle, the case closed and the mic in its fixed spot:
  1. Start recording on the Mac.
  2. sudo scripts/fan-noise-sweep.py --setup before-swap
     and press Enter when asked: that keypress is t=0.
  3. Stay quiet until it prints DONE, then stop the recording; export a WAV.
  scripts/fan-noise-sweep.py --dry-run   prints the schedule, changes nothing.

Writes (owned by the sudo user) to docs/data/acoustic/:
  fan-sweep-<setup>-<UTC>.steps.csv    a row per step: times from t=0, set duty
                                       and mean rpm per fan, the other fan
                                       headers, GPU temps (util = peak)
  fan-sweep-<setup>-<UTC>.samples.csv  a row per second

Safety (as in verify-gpu-fan-mapping.sh): gpu-fan-control is stopped for the
sweep and (re)started on ANY exit (done, error, Ctrl-C, SIGTERM, SIGQUIT, dropped
SSH; Ctrl-Z is ignored), even if it wasn't running before; the fans go back to
BIOS auto first, never to a fixed duty. No fan goes below its zone's min_duty;
aborts if a GPU core reaches 70 C or an HBM 80 C, if nvidia-smi stops
reporting a GPU's temperatures, or if anything else changes the fans
meanwhile. Won't start unless every GPU of a driven zone reports its core and
HBM temperatures (set "hbm_temp": false on a zone whose card has no memory
sensor). Even kill -9 or a hang is covered: before stopping the service it
starts a guard unit (fan-noise-sweep-guard) that starts the service again
within a second of this process dying, or once it has shown no sign of life
for 60 s; a sweep that ends normally cancels it.
A step during which a GPU got busy or a fan stalled is flagged in the CSV: its
audio isn't clean.
"""
import argparse
import csv
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

CONFIG = os.environ.get("FAN_CONFIG", "/srv/ai/scripts/gpu-fan-control.config.json")
HWMON_BASE = os.environ.get("HWMON_BASE", "/sys/class/hwmon")
OUT_DIR = "/srv/ai/docs/data/acoustic"
SERVICE = "gpu-fan-control.service"
ANALYZE = "/srv/ai/scripts/fan-noise-analyze.py"

ABORT_CORE_C = 70      # stop + restore if any GPU core reaches this ...
ABORT_HBM_C = 80       # ... or any HBM (the V100's throttles ~85 C)
BUSY_UTIL = 20         # % GPU utilization that flags a step (and blocks the start)
DEFAULT_MIN_DUTY = 30  # gpu-fan-control's own default for a zone without min_duty
ABS_MIN_DUTY = 20      # never lower, whatever a zone's min_duty says
STALL_MIN_DUTY = 25    # as the daemon: below this a fan on a voltage header may stop
STALL_SAMPLES = 5      # consecutive low-rpm samples before a step is flagged
DEFAULT_STALL_RPM = 300
PWM_TOLERANCE = 2      # pwm counts; more = something else is driving the fan
SAMPLE_SEC = 1.0
PROGRESS_SEC = 5.0
MIN_MEASURE = 5        # s; the analysis trims a few seconds off each step's window
IDLE_GAP_SEC = 1.0     # between the three pre-start idle checks
NVSMI_TIMEOUT = 10
RESTORE_WAIT = 10      # s to wait for the restarted daemon to take the fans back
# The out-of-process fail-safe (arm_guard): a transient unit that starts the fan service
# once this process is gone, or hasn't refreshed its heartbeat file for GUARD_LEASE s.
# Its fixed name doubles as a lock: systemd refuses a second sweep while one runs.
GUARD_UNIT = "fan-noise-sweep-guard"
GUARD_LEASE = 60
BEAT_DIR = "/run"      # root-only: the heartbeat file can't be planted or redirected
GPU_FIELDS = (("core_c", "core"), ("hbm_c", "hbm"), ("util_pct", "util"),
              ("power_w", "power"), ("fan_pct", "fan"))


class Abort(Exception):
    """A limit was hit: stop the sweep (the fans are then restored)."""


def say(msg=""):
    # A dead terminal (dropped SSH session) must never break the restore path.
    try:
        print(msg, flush=True)
    except OSError:
        pass


def utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_hwmon(name):
    for h in sorted(os.listdir(HWMON_BASE)):
        p = os.path.join(HWMON_BASE, h)
        try:
            with open(os.path.join(p, "name")) as f:
                if f.read().strip() == name:
                    return p
        except OSError:
            pass
    return None


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_sysfs(path, value):
    # `with` so a rejected sysfs write raises here, not at a later GC close.
    with open(path, "w") as f:
        f.write(str(value))


def manual_mode(enable, pwm):
    """nct6775 reads manual mode back as enable 1, but at pwm 255 as 0 ("off", full speed)."""
    return enable == 1 or (enable == 0 and pwm == 255)


class Zone:
    """A driven fan zone of gpu-fan-control.config.json."""

    def __init__(self, cfg):
        self.pwm_name = cfg["pwm"]
        self.name = cfg.get("name", self.pwm_name)
        self.gpus = [int(g) for g in cfg.get("gpus", [])]
        # Named by GPU, so labels stay comparable if the fan moves to another header.
        self.label = "GPU" + "+".join(map(str, self.gpus)) if self.gpus else self.pwm_name
        self.min_duty = int(cfg.get("min_duty", DEFAULT_MIN_DUTY))
        hwname = cfg.get("hwmon_name", "nct6775")
        self.hw = find_hwmon(hwname)
        if self.hw is None:
            raise SystemExit(f"{self.name}: no hwmon chip named {hwname!r}")
        self.pwm = os.path.join(self.hw, self.pwm_name)
        self.enable = self.pwm + "_enable"
        if not os.path.exists(self.pwm):
            raise SystemExit(f"{self.name}: {self.pwm} not found")
        # Same tach rule as gpu-fan-control: absent = fanN_input of pwmN, null = none.
        tach = cfg["tach"] if "tach" in cfg else f"fan{self.pwm_name[3:]}_input"
        self.tach = None
        if isinstance(tach, str) and re.fullmatch(r"fan\d+_input", tach) \
                and os.path.exists(os.path.join(self.hw, tach)):
            self.tach = os.path.join(self.hw, tach)
        sr = cfg.get("stall_rpm", DEFAULT_STALL_RPM)
        ok = isinstance(sr, int) and not isinstance(sr, bool) and sr >= 0
        self.stall_rpm = sr if ok else DEFAULT_STALL_RPM
        # Sweep-only key (the daemon ignores it): false = this card has no memory sensor.
        self.hbm_temp = cfg.get("hbm_temp", True) is not False
        self.saved_enable = None    # pwmN_enable as the daemon's exit left it (BIOS auto)
        self.raw = None             # pwm value last written

    def set_duty(self, pct):
        self.raw = round(pct * 255 / 100)
        write_sysfs(self.enable, 1)
        write_sysfs(self.pwm, self.raw)

    def rpm(self):
        return read_int(self.tach) if self.tach else None


def load_zones(path):
    with open(path) as f:
        cfg = json.load(f)
    zones = [Zone(z) for z in cfg.get("zones", []) if z.get("pwm") and not z.get("monitor_only")]
    if not zones:
        raise SystemExit(f"no driven fan zones in {path}")
    labels = [z.label for z in zones]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"zones need distinct gpus to be told apart: {labels}")
    return zones


class Step:
    def __init__(self, label, zone, set_pct, duty):
        self.label = label        # "GPU1 fan @75%"
        self.zone = zone          # a zone label, "all", "lead-in" or "lead-out"
        self.set_pct = set_pct    # {zone label: duty %}
        self.duty = duty


def build_schedule(zones, duties, hold, mode):
    def every(d):
        return {z.label: d for z in zones}
    steps = [Step(f"lead-in (all @{hold}%)", "lead-in", every(hold), hold)]
    others = [d for d in duties if d != hold]
    if mode in ("each", "both"):
        for z in zones:
            for d in others:
                s = every(hold)
                s[z.label] = d
                steps.append(Step(f"{z.label} fan @{d}%", z.label, s, d))
    if mode == "all" or (mode == "both" and len(zones) > 1):
        for d in others:
            steps.append(Step(f"all fans @{d}%", "all", every(d), d))
    steps.append(Step(f"lead-out (all @{hold}%)", "lead-out", every(hold), hold))
    return steps


def parse_duties(text):
    try:
        vals = sorted({int(v) for v in text.split(",") if v.strip()})
    except ValueError:
        raise SystemExit(f"--duties: want whole percentages like 35,50,75,100 (got {text!r})")
    if not vals or vals[0] < 1 or vals[-1] > 100:
        raise SystemExit(f"--duties: each must be 1-100 (got {text!r})")
    return vals


def nvsmi(args):
    """nvidia-smi bounded by NVSMI_TIMEOUT; a hung child is killed, not waited for."""
    p = subprocess.Popen(["nvidia-smi", *args], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True)
    try:
        out, _ = p.communicate(timeout=NVSMI_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()            # don't wait: a child stuck in the driver (D state) won't die
        raise Abort(f"nvidia-smi hung for {NVSMI_TIMEOUT} s")
    if p.returncode:
        raise Abort(f"nvidia-smi failed (exit {p.returncode})")
    return out


def _num(s):
    try:
        return float(s.strip().strip("[]"))
    except ValueError:      # "N/A", "[N/A]", "[Not Supported]"
        return None


def gpu_state():
    out = nvsmi(["--query-gpu=index,temperature.gpu,temperature.memory,utilization.gpu,"
                 "power.draw,fan.speed", "--format=csv,noheader,nounits"])
    gpus = {}
    for line in out.strip().splitlines():
        p = line.split(",")
        if len(p) >= 6 and _num(p[0]) is not None:
            gpus[int(_num(p[0]))] = {"core": _num(p[1]), "hbm": _num(p[2]), "util": _num(p[3]),
                                     "power": _num(p[4]), "fan": _num(p[5])}
    if not gpus:
        raise Abort("nvidia-smi listed no GPUs")
    return gpus


def check_limits(gpus):
    for g, s in sorted(gpus.items()):
        if s["core"] is not None and s["core"] >= ABORT_CORE_C:
            raise Abort(f"GPU{g} core {s['core']:.0f} C >= {ABORT_CORE_C} C")
        if s["hbm"] is not None and s["hbm"] >= ABORT_HBM_C:
            raise Abort(f"GPU{g} HBM {s['hbm']:.0f} C >= {ABORT_HBM_C} C")


def busy_gpus(gpus):
    return [g for g, s in sorted(gpus.items()) if s["util"] is not None and s["util"] >= BUSY_UTIL]


def chip_channels(zones):
    """Every pwmN and fanN_input of the zones' chips, BIOS-run fans included."""
    hws = sorted({z.hw for z in zones})
    out = []
    for hw in hws:
        prefix = "" if len(hws) == 1 else os.path.basename(hw) + "_"
        names = os.listdir(hw)
        pwms = sorted((n for n in names if re.fullmatch(r"pwm\d+", n)), key=lambda n: int(n[3:]))
        fans = sorted((n for n in names if re.fullmatch(r"fan\d+_input", n)),
                      key=lambda n: int(n[3:-6]))
        out += [(f"{prefix}{n}_pct", os.path.join(hw, n), True) for n in pwms]
        out += [(f"{prefix}{n[:-6]}_rpm", os.path.join(hw, n), False) for n in fans]
    return out


def systemctl(*args, timeout=90):
    return subprocess.run(["systemctl", *args], timeout=timeout, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True)


def service_active():
    try:
        return systemctl("is-active", "--quiet", SERVICE, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def chown_to_sudo_user(path=None, fd=None):
    """Hand an output to the user who ran sudo, never through a symlink."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        try:
            if fd is not None:
                os.fchown(fd, int(uid), int(gid))
            else:
                os.chown(path, int(uid), int(gid), follow_symlinks=False)
        except (OSError, ValueError):
            pass


def create_new(path):
    """A NEW file to write, as root: never an existing file, never through a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    chown_to_sudo_user(fd=fd)
    return os.fdopen(fd, "w", newline="")


def blank(v):
    return "" if v is None else v


def fmt_status(zones, row, gpus, gpu_ids):
    parts = []
    for z in zones:
        rpm = row.get(f"{z.label}_rpm")
        parts.append(f"{z.label} fan {'-' if rpm is None else rpm} rpm")
    for g in gpu_ids:
        s = gpus.get(g)
        if s and s["core"] is not None:
            hbm = "" if s["hbm"] is None else f"/{s['hbm']:.0f}"
            parts.append(f"GPU{g} {s['core']:.0f}{hbm} C")
    return "  ".join(parts)


class Sweep:
    def __init__(self, zones, steps, settle, measure, gpus, t0):
        self.zones, self.steps = zones, steps
        self.settle, self.measure = settle, measure
        self.gpu_ids = sorted(gpus)     # each must keep reporting its temperatures
        self.hbm_ids = {g for g, s in gpus.items() if s["hbm"] is not None}
        self.channels = chip_channels(zones)
        self.cols = [c for c, _, _ in self.channels] + \
            [f"gpu{g}_{k}" for g in self.gpu_ids for k, _ in GPU_FIELDS]
        self.armed = False              # the guard was armed: from then on restore() hands the fans back
        self.handed_back = False        # restore() saw the restarted service take the fans
        self.beat_path = os.path.join(BEAT_DIR, f"fan-noise-sweep-{os.getpid()}.beat")
        self.notes = []                 # printed after the sweep, as NOTE lines
        self.steps_path = self.samples_path = None
        self.steps_f = self.samples_f = None
        self.t0 = t0

    def rel(self):
        return time.monotonic() - self.t0

    def open_files(self, base, meta):
        self.steps_path, self.samples_path = base + ".steps.csv", base + ".samples.csv"
        self.steps_f = create_new(self.steps_path)
        self.samples_f = create_new(self.samples_path)
        for line in meta:
            self.steps_f.write(f"# {line}\n")
        zone_cols = [f"{z.label}_{k}" for z in self.zones for k in ("set_pct", "rpm")]
        self.steps_w = csv.writer(self.steps_f)
        self.steps_w.writerow(["step", "label", "zone", "duty_pct", "t_start_s", "t_measure_s",
                               "t_end_s", *zone_cols, *self.cols, "busy", "stalled"])
        self.samples_w = csv.writer(self.samples_f)
        self.samples_w.writerow(["t_s", "utc", "step", *self.cols])
        self.steps_f.flush()
        self.samples_f.flush()

    def close_files(self, note=None):
        for f in (self.steps_f, self.samples_f):
            if f is None:
                continue
            try:
                if note and f is self.steps_f:
                    f.write(f"# {note}\n")
                f.close()
            except OSError:
                pass

    def arm_guard(self):
        """The fail-safe for what restore() can't handle: a kill -9 or OOM kill, or a sweep
        hung with the fans held (say, blocked writing to a terminal paused by Ctrl-S). A
        transient unit watches this process and the heartbeat file sample() refreshes, and
        starts the fan service once the process is gone or the heartbeat is GUARD_LEASE s
        old. Not armed = nothing touched. Known gaps, not fixed: docs/server-setup.md,
        "Fan noise test"."""
        ctl = shutil.which("systemctl")
        if ctl is None:
            raise Abort("systemctl not found; nothing was changed")
        try:    # before the guard starts: it takes a missing heartbeat for a dead sweep
            fd = os.open(self.beat_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.utime(fd)
            finally:
                os.close(fd)
        except OSError as e:
            raise Abort(f"can't create the heartbeat file {self.beat_path} ({e}); nothing was changed")
        beat = shlex.quote(self.beat_path)
        # Any failing test ends the loop and fires, a heartbeat that can't be read included.
        watch = (f"while kill -0 {os.getpid()} 2>/dev/null && now=$(date +%s) && "
                 f"t=$(stat -c %Y {beat} 2>/dev/null) && [ $((now - t)) -lt {GUARD_LEASE} ]; "
                 f"do sleep 1; done; rm -f {beat}; exec {shlex.quote(ctl)} --no-block start {SERVICE}")
        cmd = ["systemd-run", "--quiet", "--collect", f"--unit={GUARD_UNIT}", "--property=Type=exec",
               f"--description=fan-noise-sweep fail-safe: starts {SERVICE} if the sweep dies or hangs",
               "/bin/sh", "-c", watch]
        try:
            r = subprocess.run(cmd, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            err = (r.stderr.strip() or f"exit {r.returncode}") if r.returncode else None
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)
        if err:
            self.drop_beat()
            raise Abort(f"could not arm the fail-safe guard {GUARD_UNIT}.service ({err}); nothing was "
                        f"changed. Is another sweep running? With none running, sudo systemctl stop "
                        f"{GUARD_UNIT}.service clears it")

    def beat(self):
        """A sign of life for the guard. A failure is ignored: the guard then fires, the
        service starts, and the ownership check stops the sweep."""
        try:
            os.utime(self.beat_path, follow_symlinks=False)
        except OSError:
            pass

    def drop_beat(self):
        try:
            os.unlink(self.beat_path)
        except OSError:
            pass

    def disarm_guard(self):
        """Cancel the guard once the service is back. Failing to is harmless: the guard
        fires as this process exits, and starting a running service does nothing."""
        try:
            r = systemctl("stop", f"{GUARD_UNIT}.service", timeout=30)
            # 5 = not loaded: it fired already (the sweep hung a while) and started the service
            err = None if r.returncode in (0, 5) else (r.stderr.strip() or f"exit {r.returncode}")
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)
        if err:
            self.notes.append(f"couldn't cancel the fail-safe guard {GUARD_UNIT}.service ({err}): as this "
                              f"sweep exits it starts {SERVICE}, which is running already: harmless")
        else:
            self.drop_beat()

    def take_over(self):
        self.arm_guard()            # first: if it can't be armed, nothing is touched
        self.armed = True           # from here on restore() hands the fans back, whatever happens
        if not service_active():
            say(f"NOTE: {SERVICE} isn't running; the sweep starts it when it ends.")
        say(f"Stopping {SERVICE} for the sweep...")
        r = systemctl("stop", SERVICE)
        if r.returncode:            # no fan touched yet
            raise Abort(f"could not stop {SERVICE}: {r.stderr.strip() or r.returncode}")
        for z in self.zones:
            z.saved_enable = read_int(z.enable)

    def restore(self):
        """Fans to BIOS auto, restart the fan service, cancel the guard. Returns the problems."""
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGTSTP):
            signal.signal(s, signal.SIG_IGN)
        problems = []
        if not self.armed:          # refused before anything was touched
            self.drop_beat()        # if a signal cut arm_guard() short
            return problems
        for z in self.zones:
            # BIOS auto, as the daemon's own exit leaves them. A manual mode (1) found at the
            # start is never written back: with the service down it would hold a fixed duty.
            for mode in ([z.saved_enable] if z.saved_enable not in (None, 1) else []) + [5, 2]:
                try:
                    write_sysfs(z.enable, mode)
                    break
                except OSError:
                    continue
            else:
                try:
                    write_sysfs(z.pwm, 255)
                    problems.append(f"{z.pwm_name}: BIOS won't take it back; left at 100%")
                except OSError as e:
                    problems.append(f"{z.pwm_name}: could not restore ({e})")
        # Even if the stop failed or never came, or the service wasn't running before.
        # Restart, not start: if anything started the daemon meanwhile, the enable writes
        # above took the fans from it, and only its startup takes them back.
        try:
            systemctl("restart", SERVICE)
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"systemctl restart {SERVICE} failed: {e}")
        deadline = time.monotonic() + RESTORE_WAIT
        while True:
            active = service_active()
            took = active and all(manual_mode(read_int(z.enable), read_int(z.pwm)) for z in self.zones)
            if took or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        if not active:
            problems.append(f"{SERVICE} is NOT running (journalctl -u {SERVICE})")
        elif not took:
            problems.append(f"{SERVICE} is running but hasn't taken the fans back "
                            f"(BIOS auto meanwhile; journalctl -u {SERVICE})")
        self.handed_back = took
        if took:
            self.disarm_guard()
        else:                       # one more try: the guard fires as this process exits
            self.notes.append(f"the fail-safe guard {GUARD_UNIT}.service stays armed: as this sweep "
                              f"exits it starts {SERVICE} if it isn't running")
        return problems

    def check_ownership(self):
        for z in self.zones:
            if z.raw is None:
                continue
            en, raw = read_int(z.enable), read_int(z.pwm)
            if not manual_mode(en, raw) or raw is None or abs(raw - z.raw) > PWM_TOLERANCE:
                raise Abort(f"{z.pwm_name} changed underneath the sweep (enable {en}, pwm {raw}, "
                            f"set {z.raw}): is {SERVICE} running again?")

    def check_telemetry(self, gpus):
        """A temperature nvidia-smi stops reporting can't be watched: stop."""
        for g in self.gpu_ids:
            s = gpus.get(g)
            if s is None:
                raise Abort(f"nvidia-smi no longer lists GPU{g}")
            if s["core"] is None or (g in self.hbm_ids and s["hbm"] is None):
                raise Abort(f"nvidia-smi stopped reporting GPU{g}'s temperature")

    def sample(self, step_no):
        self.beat()
        gpus = gpu_state()
        t = self.rel()
        row = {}
        for col, path, is_pwm in self.channels:
            v = read_int(path)
            row[col] = None if v is None else (round(v * 100 / 255, 1) if is_pwm else v)
        for g in self.gpu_ids:
            s = gpus.get(g, {})
            for col, key in GPU_FIELDS:
                row[f"gpu{g}_{col}"] = s.get(key)
        for z in self.zones:
            row[f"{z.label}_rpm"] = z.rpm()
        self.samples_w.writerow([f"{t:.1f}", utc_iso(), step_no, *(blank(row[c]) for c in self.cols)])
        self.samples_f.flush()
        self.check_telemetry(gpus)
        check_limits(gpus)
        self.check_ownership()
        return t, row, gpus

    def run_step(self, n, step):
        for z in self.zones:
            z.set_duty(step.set_pct[z.label])
        t_start = self.rel()
        t_meas, t_end = t_start + self.settle, t_start + self.settle + self.measure
        say(f"[{n}/{len(self.steps)}] {step.label}")
        window, busy, stalled = [], set(), set()
        low = dict.fromkeys((z.label for z in self.zones), 0)
        tick, next_note = t_start, t_start + PROGRESS_SEC
        while True:
            t, row, gpus = self.sample(n)
            if t >= t_meas:
                window.append(row)
            for g in busy_gpus(gpus):
                if g not in busy:
                    busy.add(g)
                    say(f"    GPU{g} got busy ({gpus[g]['util']:.0f}% util): step flagged")
            for z in self.zones:
                rpm = row[f"{z.label}_rpm"]
                if rpm is None or z.stall_rpm == 0 or step.set_pct[z.label] < STALL_MIN_DUTY:
                    continue
                low[z.label] = low[z.label] + 1 if rpm < z.stall_rpm else 0
                if low[z.label] >= STALL_SAMPLES and z.label not in stalled:
                    stalled.add(z.label)
                    say(f"    {z.label} fan at {rpm} rpm: STALLED, step flagged")
            if t >= next_note:
                next_note += PROGRESS_SEC
                phase = "measuring" if t >= t_meas else "settling "
                say(f"    {t - t_start:3.0f} s {phase}  {fmt_status(self.zones, row, gpus, self.gpu_ids)}")
            if t >= t_end:
                break
            tick += SAMPLE_SEC
            time.sleep(max(0.0, tick - self.rel()))

        def avg(key):
            vals = [r[key] for r in window if r.get(key) is not None]
            if not vals:
                return ""
            if key.endswith("_util_pct"):
                return f"{max(vals):g}"
            return f"{sum(vals) / len(vals):.1f}"
        zone_vals = []
        for z in self.zones:
            zone_vals += [step.set_pct[z.label], avg(f"{z.label}_rpm")]
        self.steps_w.writerow([n, step.label, step.zone, step.duty, f"{t_start:.2f}", f"{t_meas:.2f}",
                               f"{t_end:.2f}", *zone_vals, *(avg(c) for c in self.cols),
                               " ".join(f"GPU{g}" for g in sorted(busy)), " ".join(sorted(stalled))])
        self.steps_f.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", default="", help="cooling setup name for the file names, e.g. before-swap")
    ap.add_argument("--duties", default="35,50,75,100", help="fan duties in %% (default 35,50,75,100)")
    ap.add_argument("--hold", type=int, help="duty of the fans not being stepped (default: lowest duty)")
    ap.add_argument("--mode", choices=("both", "each", "all"), default="both",
                    help="each = one fan at a time, all = all fans together, both (default)")
    ap.add_argument("--settle", type=float, default=15, help="seconds to reach speed (default 15)")
    ap.add_argument("--measure", type=float, default=45, help="seconds measured per step (default 45)")
    ap.add_argument("--out", default=OUT_DIR, help=f"output directory (default {OUT_DIR})")
    ap.add_argument("--dry-run", action="store_true", help="print the schedule; change nothing")
    ap.add_argument("--no-wait", action="store_true", help="start without waiting for Enter")
    args = ap.parse_args(argv)

    duties = parse_duties(args.duties)
    hold = duties[0] if args.hold is None else args.hold
    if args.settle < 0 or args.measure < MIN_MEASURE:
        raise SystemExit(f"--settle must be >= 0 and --measure >= {MIN_MEASURE:g}")
    zones = load_zones(CONFIG)
    for z in zones:
        floor = max(z.min_duty, ABS_MIN_DUTY)
        low = [d for d in duties + [hold] if d < floor]
        if low:
            raise SystemExit(f"{min(low)}% is below the {z.label} fan's floor of {floor}% "
                             f"(min_duty in {CONFIG})")
    if hold > 100:
        raise SystemExit("--hold must be <= 100")
    setup = re.sub(r"[^A-Za-z0-9._-]+", "-", args.setup).strip("-")
    steps = build_schedule(zones, duties, hold, args.mode)

    per = args.settle + args.measure
    say(f"Fan zones ({CONFIG}):")
    for z in zones:
        tach = os.path.basename(z.tach) if z.tach else "none"
        hbm = "" if z.hbm_temp else ", no HBM sensor"
        say(f"  {z.label} fan: {z.pwm_name}, tach {tach}, min {z.min_duty}%{hbm}  ({z.name})")
    say(f"Schedule: {len(steps)} steps x {per:g} s ({args.settle:g} settle + {args.measure:g} "
        f"measure) = {len(steps) * per / 60:.1f} min")
    for i, s in enumerate(steps, 1):
        say(f"  {i:2d}. {s.label}")

    def idle_check():
        """The GPUs as the sweep needs them; an Abort says what's wrong and what to do."""
        gpus = gpu_state()
        blind = [g for g, s in sorted(gpus.items()) if s["core"] is None]
        if blind:
            raise Abort("nvidia-smi reports no core temperature for "
                        + ", ".join(f"GPU{g}" for g in blind) + " (the sweep watches every GPU's)")
        for z in zones:             # each driven fan's GPUs: listed, every sensor reporting
            for g in z.gpus:
                if g not in gpus:
                    raise Abort(f"nvidia-smi doesn't list GPU{g}, whose fan the sweep drives")
                if z.hbm_temp and gpus[g]["hbm"] is None:
                    raise Abort(f"nvidia-smi reports no HBM temperature for GPU{g}, so the sweep "
                                f"couldn't watch it. If that card has no memory sensor, set "
                                f"\"hbm_temp\": false on its zone in {CONFIG}")
        try:
            check_limits(gpus)
        except Abort as e:
            raise Abort(f"{e}: let it cool down and run it again")
        busy = busy_gpus(gpus)
        if busy:
            raise Abort("busy: " + ", ".join(f"GPU{g} {gpus[g]['util']:.0f}%" for g in busy)
                        + ": wait for the job to finish and run it again")
        return gpus

    if args.dry_run:
        try:
            idle_check()
            say("GPUs idle and cool: OK.")
        except Abort as e:
            say(f"Not ready: {e}. The sweep would refuse to start.")
        say(f"{SERVICE}: " + ("running (stopped for the sweep, then restarted)" if service_active()
                              else "NOT running (the sweep starts it when it ends)"))
        return 0
    if os.geteuid() != 0:
        raise SystemExit("Run it with sudo (or --dry-run).")
    out = os.path.normpath(args.out)    # no trailing "/": islink() must see the link itself
    if os.path.islink(out):
        raise SystemExit(f"--out {out} is a symlink: give the real directory.")

    def require_idle(checks):
        try:
            for k in range(checks):
                if k:
                    time.sleep(IDLE_GAP_SEC)
                gpus = idle_check()
        except Abort as e:
            raise SystemExit(f"Not starting: {e}.")
        return gpus
    gpus = require_idle(3)

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(s, on_signal)
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)   # Ctrl-Z would freeze it with the fans held

    say(f"\nThe fan service stops for the sweep (the status page shows a fan alert until\n"
        f"it's back) and restarts at the end; if this process dies, or hangs for\n"
        f"{GUARD_LEASE} s, a guard starts it again. Aborts if a GPU core reaches {ABORT_CORE_C} C or\n"
        f"an HBM {ABORT_HBM_C} C. Ctrl-C stops it and hands the fans back.")
    if not args.no_wait:
        say("  1) Start recording on the Mac.\n  2) Press Enter here: that's t=0.\n"
            "Then keep quiet until it prints DONE.")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nCancelled; nothing was changed.")
        t0, t0_utc = time.monotonic(), datetime.now(timezone.utc)
        gpus = require_idle(1)      # a job may have started while the mic was set up
    else:
        t0, t0_utc = time.monotonic(), datetime.now(timezone.utc)

    try:
        if not os.path.isdir(out):
            os.makedirs(out)
            chown_to_sudo_user(out)
    except OSError as e:
        raise SystemExit(f"Not starting: can't create {out} ({e}). Nothing was changed.")
    sweep = Sweep(zones, steps, args.settle, args.measure, gpus, t0)
    base = os.path.join(out, f"fan-sweep-{setup + '-' if setup else ''}"
                             f"{t0_utc.strftime('%Y%m%dT%H%M%SZ')}")
    zone_map = "; ".join(f"{z.label}={z.pwm_name}/{os.path.basename(z.tach) if z.tach else '-'} "
                         f"({z.name})" for z in zones)
    meta = [f"fan-noise-sweep setup={setup or '-'} t0_utc={t0_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"duties={','.join(map(str, duties))} hold={hold} mode={args.mode} "
            f"settle_s={args.settle:g} measure_s={args.measure:g}",
            f"zones: {zone_map}",
            "step values: means over [t_measure_s, t_end_s]; util_pct is the peak"]
    try:
        sweep.open_files(base, meta)
    except OSError as e:
        sweep.close_files()
        raise SystemExit(f"Not starting: can't create the output files ({e}). Nothing was changed.")
    note, rc, problems = None, 0, []
    try:
        sweep.take_over()
        for n, step in enumerate(steps, 1):
            sweep.run_step(n, step)
    except Abort as e:
        note, rc = f"ABORTED: {e}", 2
    except KeyboardInterrupt:
        note, rc = "ABORTED: Ctrl-C", 130
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
        note = f"ABORTED: signal {rc - 128}" if rc > 128 else f"ABORTED: {e}"
    except Exception as e:      # noqa: BLE001 -- any failure: restore first, then report
        note, rc = f"ABORTED: {type(e).__name__}: {e}", 1
    finally:
        while True:             # a signal landing before restore() ignores them: redo it
            try:
                problems = sweep.restore()
                break
            except (KeyboardInterrupt, SystemExit):
                continue
        at = f" at t={sweep.rel():.0f} s"
        sweep.close_files(note and note + at)
        say()
        if note:
            say(note + at)
        for p in problems:
            say(f"WARNING: {p}")
        if problems:
            say(f"Fix by hand:  sudo systemctl restart {SERVICE}")
        elif sweep.handed_back:
            say(f"Fans handed back: {SERVICE} is running them again.")
        for n in sweep.notes:
            say(f"NOTE: {n}")
        if sweep.samples_f is not None:
            say(("DONE" if not note else "STOPPED") + ": stop the recording and export it as WAV.")
            say(f"  steps:   {sweep.steps_path}\n  samples: {sweep.samples_path}")
            say(f"Analyze:  {ANALYZE} --room ROOM.wav \\\n"
                f"            --steps {sweep.steps_path} SWEEP.wav")
    return rc or (3 if problems else 0)


if __name__ == "__main__":
    sys.exit(main())

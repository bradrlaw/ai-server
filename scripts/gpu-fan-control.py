#!/usr/bin/env python3
"""GPU-temp-driven fan control for the headless AI server.

Reads each GPU's temperature via nvidia-smi and drives the motherboard
Nuvoton (nct6775) 4-pin PWM fan headers that the GPU shroud fans are wired to.

The passively-cooled Tesla cards (2x V100, 1x P100) have NO onboard fan control,
so their user-added shroud fans MUST be driven externally -- this daemon does it.

Config: /srv/ai/scripts/gpu-fan-control.config.json  (see zones below).
Run as root (writes /sys/class/hwmon/.../pwmN). Install via the systemd unit.

Fail-safe: on ANY error or on exit, fans are forced to 100% (manual) or handed
back to the BIOS, so a crash never leaves the cards without airflow.
"""
import json
import os
import signal
import subprocess
import sys
import time

CONFIG = os.environ.get("FAN_CONFIG", "/srv/ai/scripts/gpu-fan-control.config.json")


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


def gpu_temps():
    """Return {gpu_index: {'core','mem','eff'}} from nvidia-smi.

    On the Tesla V100 the HBM2 memory (temperature.memory) runs ~15-20 C hotter
    than the GPU core and throttles at ~85 C -- so the fan MUST be driven off the
    memory temperature, not the core. We control on eff = max(core, mem). The P100
    reports temperature.memory as N/A, so it falls back to core only.

    We also read fan.speed: the passive datacenter cards (P100/V100) report N/A
    (they are cooled only by the chassis shroud fans this daemon drives), but a
    consumer card that self-cools with its own onboard fan (e.g. the GTX Titan X
    stopgap in idx0) reports its fan duty -- we surface that in the log so its
    self-managed cooling is visible.
    """
    out = subprocess.check_output(
        ["nvidia-smi",
         "--query-gpu=index,temperature.gpu,temperature.memory,fan.speed",
         "--format=csv,noheader,nounits"], text=True)
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
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL)
        return {int(x.strip()) for x in out.strip().splitlines() if x.strip() != ""}
    except Exception:
        return set()


def current_power_limits():
    """Return {gpu_index: enforced_power_limit_W} for GPUs nvidia-smi can see."""
    limits = {}
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,power.limit",
             "--format=csv,noheader,nounits"], text=True, stderr=subprocess.DEVNULL)
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


def reconcile_power_limits(power_limits, quiet=False):
    """Idempotently enforce per-GPU power caps (W) + persistence mode.

    power_limits: {gpu_index(str|int): watts}. For longevity/thermal reasons the
    passively-cooled Teslas are capped below their default board limit (V100 HBM2
    pegs 85 C at stock 250 W; 175 W keeps it ~83-84 C at ~91% throughput; the P100
    has headroom and is trimmed to 200 W for ~0% loss). Persistence mode keeps the
    cap sticky and the driver resident on this headless box.

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
            subprocess.run(["nvidia-smi", "-i", str(idx), "-pm", "1"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["nvidia-smi", "-i", str(idx), "-pl", str(watts)],
                           check=True, capture_output=True, text=True)
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
        # consumer GTX Titan X, whose built-in fan replaces the passive P100's
        # chassis pump fan). Any pwm listed is only used to hand that header back
        # to BIOS auto at startup so we stop forcing an unused fan.
        self.monitor_only = cfg.get("monitor_only", False)
        self.curve = sorted(cfg.get("curve", []))
        self.pwm = None
        self.enable = None
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

    def set_enable(self, mode):
        if self.enable is None:
            return
        try:
            open(self.enable, "w").write(str(mode))
        except OSError as e:
            log(f"{self.name}: cannot set {self.enable}={mode}: {e}")

    def write_duty(self, duty_pct):
        duty_pct = max(self.min_duty, min(100, duty_pct))
        raw = int(round(duty_pct / 100 * 255))
        open(self.pwm, "w").write(str(raw))
        return duty_pct

    def update(self, temps):
        # eff = max(core, memory) across this zone's GPUs; memory is the V100
        # throttle limiter and runs much hotter than the core under load.
        vals = [temps.get(i, {"core": 0, "mem": None, "eff": 0, "fan": None}) for i in self.gpus]
        t = max((v["eff"] for v in vals), default=0)
        core = max((v["core"] for v in vals), default=0)
        mem = max((v["mem"] for v in vals if v["mem"] is not None), default=None)
        gpufan = next((v["fan"] for v in vals if v.get("fan") is not None), None)
        label = f"{core}C" if mem is None else f"c{core}/m{mem}C"
        # A self-cooled card (consumer GPU with its own fan) reports its fan duty;
        # passive P100/V100 report N/A. Surface it so the card's own cooling shows.
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
        duty = self.write_duty(interp(self.curve, t))
        return label, duty

    def full_speed(self):
        if self.monitor_only or self.pwm is None:
            return
        self.set_enable(1)
        try:
            self.write_duty(100)
        except OSError:
            pass

    def restore_auto(self):
        if self.enable is None:
            return
        # 5 = nct6775 "smart fan"/BIOS auto; fall back to 2, then full speed.
        for mode in (5, 2):
            try:
                open(self.enable, "w").write(str(mode))
                return
            except OSError:
                continue
        self.full_speed()


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
    reconcile_power_limits(power_limits)
    last_power_check = time.time()
    for z in zones:
        if z.monitor_only:
            z.restore_auto()  # hand any listed header back to BIOS; we only log
        else:
            z.set_enable(1)  # manual PWM
    log(f"controlling {len(zones)} zone(s), interval {interval}s, "
        f"power recheck {power_recheck}s")

    stop = {"flag": False}

    def handle(signum, _frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    def panic_full_speed():
        for z in zones:
            z.full_speed()

    try:
        while not stop["flag"]:
            # Periodically re-enforce power caps so a GPU that was missing at
            # boot or fell off the bus and returned gets capped automatically.
            if time.time() - last_power_check >= power_recheck:
                reconcile_power_limits(power_limits)
                last_power_check = time.time()
            try:
                temps = gpu_temps()
            except Exception as e:
                log(f"nvidia-smi failed ({e}) -> fans to 100%")
                panic_full_speed()
                time.sleep(interval)
                continue
            status = []
            for z in zones:
                try:
                    label, duty = z.update(temps)
                    if duty is None:
                        status.append(f"{z.name}:{label}[monitor]")
                    else:
                        status.append(f"{z.name}:{label}->{duty:.0f}%")
                except Exception as e:
                    log(f"{z.name} update error ({e}) -> 100%")
                    z.full_speed()
            log(" ".join(status))
            time.sleep(interval)
    except Exception as e:
        log(f"FATAL {e} -> fans to 100%")
        panic_full_speed()
        raise
    finally:
        # graceful shutdown: hand fans back to the BIOS
        for z in zones:
            z.restore_auto()
        log("exited; fans returned to automatic control")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("must run as root")
    main()

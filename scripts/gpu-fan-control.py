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
    """
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=index,temperature.gpu,temperature.memory",
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
        eff = max(core, mem) if mem is not None else core
        temps[idx] = {"core": core, "mem": mem, "eff": eff}
    return temps


def apply_power_limits(power_limits):
    """Apply per-GPU power caps (W) + persistence mode at startup.

    power_limits: {gpu_index(str|int): watts}. For longevity/thermal reasons the
    passively-cooled Teslas are capped below their default board limit (V100 HBM2
    pegs 85 C at stock 250 W; 175 W keeps it ~83-84 C at ~91% throughput; the P100
    has headroom and is trimmed to 200 W for ~0% loss). Persistence mode keeps the
    cap sticky and the driver resident on this headless box.

    Non-fatal: a failure here must NEVER stop the fan daemon -- airflow is the
    safety-critical function, power capping is only an optimization.
    """
    if not power_limits:
        return
    for idx, watts in sorted(power_limits.items(), key=lambda kv: int(kv[0])):
        idx = int(idx)
        try:
            watts = int(round(float(watts)))
        except (TypeError, ValueError):
            log(f"power cap for GPU{idx}: invalid value {watts!r}, skipping")
            continue
        try:
            subprocess.run(["nvidia-smi", "-i", str(idx), "-pm", "1"],
                           check=True, capture_output=True, text=True)
            subprocess.run(["nvidia-smi", "-i", str(idx), "-pl", str(watts)],
                           check=True, capture_output=True, text=True)
            log(f"power cap GPU{idx} -> {watts}W (persistence on)")
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
        self.curve = sorted(cfg["curve"])
        self.hysteresis = cfg.get("hysteresis_c", 3)
        self.min_duty = cfg.get("min_duty", 30)
        hw = find_hwmon(cfg.get("hwmon_name", "nct6775"))
        if hw is None:
            raise RuntimeError(f"hwmon '{cfg.get('hwmon_name')}' not found")
        self.pwm = os.path.join(hw, cfg["pwm"])
        self.enable = self.pwm + "_enable"
        if "REPLACE_ME" in cfg["pwm"] or not os.path.exists(self.pwm):
            raise RuntimeError(
                f"zone '{self.name}': pwm channel '{cfg['pwm']}' not set/found "
                f"({self.pwm}). Run identify-fan.sh and edit the config.")
        self._last_temp = None

    def set_enable(self, mode):
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
        vals = [temps.get(i, {"core": 0, "mem": None, "eff": 0}) for i in self.gpus]
        t = max((v["eff"] for v in vals), default=0)
        core = max((v["core"] for v in vals), default=0)
        mem = max((v["mem"] for v in vals if v["mem"] is not None), default=None)
        # hysteresis: only react if temp moved enough
        if self._last_temp is not None and abs(t - self._last_temp) < self.hysteresis:
            t = self._last_temp
        else:
            self._last_temp = t
        duty = self.write_duty(interp(self.curve, t))
        label = f"{core}C" if mem is None else f"c{core}/m{mem}C"
        return label, duty

    def full_speed(self):
        self.set_enable(1)
        try:
            self.write_duty(100)
        except OSError:
            pass

    def restore_auto(self):
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
    zones = [Zone(z) for z in cfg["zones"]]
    apply_power_limits(cfg.get("power_limits"))
    for z in zones:
        z.set_enable(1)  # manual PWM
    log(f"controlling {len(zones)} zone(s), interval {interval}s")

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

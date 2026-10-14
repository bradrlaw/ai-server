#!/usr/bin/env python3
"""Power-cap thermal sweep for the GPUs (llama-bench prefill+decode workload).

For each cap: set the cap THROUGH gpu-fan-control (rewrite `power_limits` in its
config; the daemon re-reads it every `power_recheck_sec` and applies it live with
`nvidia-smi -pl`), wait until the driver reports it enforced, cool the cards, then
run llama-bench on every target GPU at once (lockstep, default) or one at a time
(--sequential). Every --interval seconds it samples core/HBM temp, SM clock, power,
throttle reasons, and each card's fan RPM + PWM duty (fan zones read from the same
config), plus SYSTIN as a case-ambient proxy. After each run it measures how long
each card takes to cool back to --cool-to (a direct shroud-effectiveness metric).

Why not raw `nvidia-smi -pl` (the old power-cap-sweep.sh): the daemon re-applies
its configured cap on drift every 30 s, so a raw cap silently reverts mid-run.
Going through the config needs no root (the file is brad-owned) and the daemon
already holds persistence mode. Config edits are surgical (only the target GPUs'
keys), atomic, and serialized with a directory flock so two sweeps can't clobber
each other. The original caps of the target GPUs are restored on exit (normal,
error, Ctrl-C, SIGTERM).

Safety: refuses to start on a busy GPU (utilization) or one with VRAM in use
(use --free to unload the llama-swap models pinned to the target GPUs; they are
re-warmed via `llama-swap-mode.py set <current mode>` at the end). A run is killed
if HBM reaches --hbm-ceiling. --dry-run only prints the plan + preflight.

Outputs (default docs/data/thermal/): <stamp>[-label].csv (one row per cap x GPU x
phase) and <stamp>[-label]-samples.csv (raw time series for plotting).

Cooling A/B (two shroud designs, one per V100), e.g.:
  scripts/power-cap-sweep.py --gpus 1 2 --caps 175 225 250 --free --label dual40-idx1_blower-idx2
  scripts/power-cap-sweep.py --gpus 1 2 --caps 175 --soak 600 --free --label dual40-idx1_blower-idx2
then swap the shrouds between the cards and repeat (removes the slot-position confound).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import glob
import json
import os
import re
import shlex
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

REPO = "/srv/ai"
FAN_CONFIG = os.environ.get("FAN_CONFIG", f"{REPO}/scripts/gpu-fan-control.config.json")
FAN_SERVICE = "gpu-fan-control"
LLAMA_SWAP = os.environ.get("LLAMASWAP_URL", "http://127.0.0.1:9090")
MODE_TOOL = f"{REPO}/scripts/llama-swap-mode.py"
DEFAULT_BIN = f"{REPO}/src/llama.cpp/build/bin/llama-bench"
DEFAULT_OUT = f"{REPO}/docs/data/thermal"
MODEL_CANDIDATES = (
    f"{REPO}/models/qwen3.8-27b/Qwen3.8-27B-Q6_K.gguf",      # current `coding` weights (NVMe)
    f"{REPO}/models/cold/qwen3.6-27b/Qwen3.6-27B-Q6_K.gguf",  # 2026-07-01 sweep model (HDD)
)
SMI_FIELDS = ("index,temperature.gpu,temperature.memory,clocks.sm,power.draw,power.limit,"
              "utilization.gpu,clocks_event_reasons.sw_power_cap,"
              "clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_thermal_slowdown,"
              "clocks_event_reasons.hw_slowdown")
SUMMARY_COLS = ["utc_start", "label", "mode", "phase", "gpu", "cap_w", "model", "pp", "ng", "reps",
                "pp_ts", "tg_ts", "iterations", "duration_s", "peak_hbm_c", "peak_core_c",
                "steady_hbm_c", "steady_core_c", "mean_power_w", "min_sm_mhz", "median_sm_mhz",
                "fan_rpm_max", "fan_rpm_mean", "duty_max_pct", "pct_sw_power_cap",
                "pct_thermal_slowdown", "pct_hw_slowdown", "systin_mean_c", "cool_s", "status"]
SAMPLE_COLS = ["t_s", "utc", "phase", "cap_w", "gpu", "running", "core_c", "hbm_c", "sm_mhz",
               "power_w", "plimit_w", "util_pct", "sw_power_cap", "sw_thermal", "hw_thermal",
               "hw_slowdown", "fan_rpm", "duty_pct", "systin_c"]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%SZ}] {msg}", flush=True)


def run(cmd: list[str], timeout: float = 20) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def num(v: str):
    v = v.strip()
    try:
        return float(v)
    except ValueError:
        return None  # "[N/A]", "N/A", ""


# ------------------------------------------------------------- fan config edits
class ConfigLock:
    """flock on the config's DIRECTORY: os.replace swaps the file inode, so a lock
    on the file itself wouldn't serialize writers."""

    def __enter__(self):
        self.fd = os.open(os.path.dirname(FAN_CONFIG), os.O_RDONLY)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def load_config() -> dict:
    with open(FAN_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _edit_power_limits(text: str, updates: dict[int, int]) -> str:
    """Surgically rewrite existing keys inside the `power_limits` object only (same
    approach as the status dashboard) so the hand-formatted config isn't churned."""
    m = re.search(r'"power_limits"\s*:\s*\{', text)
    if not m:
        raise RuntimeError("power_limits block not found in config")
    start, depth, end = m.end() - 1, 0, None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise RuntimeError("malformed power_limits block")
    block = text[start:end]
    for idx, watts in updates.items():
        key_re = re.compile(r'("%d"\s*:\s*)\d+' % idx)
        if not key_re.search(block):
            raise RuntimeError(f"GPU{idx} has no power_limits entry")
        block = key_re.sub(lambda mm: f"{mm.group(1)}{watts}", block, count=1)
    new = text[:start] + block + text[end:]
    got = json.loads(new)["power_limits"]  # never write a broken config
    assert all(int(got[str(i)]) == w for i, w in updates.items())
    return new


def set_caps(updates: dict[int, int]) -> None:
    with ConfigLock():
        st = os.stat(FAN_CONFIG)
        with open(FAN_CONFIG, encoding="utf-8") as f:
            new = _edit_power_limits(f.read(), updates)
        fd, tmp = tempfile.mkstemp(prefix=".gpu-fan-control.", suffix=".tmp",
                                   dir=os.path.dirname(FAN_CONFIG))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, st.st_mode & 0o7777)
            if os.geteuid() == 0:  # keep it brad-owned so the dashboard can still write it
                os.chown(tmp, st.st_uid, st.st_gid)
            os.replace(tmp, FAN_CONFIG)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def live_limits() -> dict[int, int]:
    out = run(["nvidia-smi", "--query-gpu=index,power.limit", "--format=csv,noheader,nounits"])
    res = {}
    for line in out.strip().splitlines():
        i, w = line.split(",")
        if num(w) is not None:
            res[int(i)] = int(round(float(w)))
    return res


def wait_enforced(targets: dict[int, int], timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        live = live_limits()
        if all(live.get(i) == w for i, w in targets.items()):
            return True
        if time.time() > deadline:
            return False
        time.sleep(1)


# ------------------------------------------------------------- telemetry
def _cat(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


class Fans:
    """Per-GPU fan tach + PWM duty from the daemon's zones (hwmon found by name,
    as the daemon does — hwmonN numbering isn't stable across boots)."""

    def __init__(self, cfg: dict, gpus: list[int]):
        self.paths: dict[int, tuple[str | None, str | None]] = {}
        self.systin = None
        hw_cache: dict[str, str | None] = {}
        for z in cfg.get("zones", []):
            name = z.get("hwmon_name", "nct6775")
            if name not in hw_cache:
                hw_cache[name] = next((os.path.dirname(p) for p in glob.glob("/sys/class/hwmon/hwmon*/name")
                                       if _cat(p) == name), None)
            hw = hw_cache[name]
            if not hw:
                continue
            if self.systin is None:
                self.systin = next((p.replace("_label", "_input")
                                    for p in glob.glob(f"{hw}/temp*_label")
                                    if _cat(p) == "SYSTIN"), None)
            if z.get("monitor_only"):
                continue
            n = re.sub(r"\D", "", z.get("pwm", ""))
            for g in z.get("gpus", []):
                if g in gpus and n:
                    self.paths[g] = (f"{hw}/fan{n}_input", f"{hw}/pwm{n}")

    @staticmethod
    def _read(p):
        try:
            with open(p) as f:
                return int(f.read().strip())
        except (OSError, ValueError, TypeError):
            return None

    def read(self, gpu: int):
        fan_p, pwm_p = self.paths.get(gpu, (None, None))
        rpm, pwm = self._read(fan_p), self._read(pwm_p)
        return rpm, (round(pwm * 100 / 255) if pwm is not None else None)

    def systin_c(self):
        v = self._read(self.systin)
        return v / 1000 if v is not None else None

    def describe(self, gpu: int) -> str:
        fan_p, pwm_p = self.paths.get(gpu, (None, None))
        if not pwm_p:
            return "no fan zone"
        rpm, duty = self.read(gpu)
        return f"{os.path.basename(pwm_p)}/{os.path.basename(fan_p)} {duty}% {rpm} rpm"


class Sampler(threading.Thread):
    def __init__(self, gpus: list[int], fans: Fans, interval: float):
        super().__init__(daemon=True)
        self.gpus, self.fans, self.interval = gpus, fans, interval
        self.t0 = time.monotonic()
        self.samples: list[dict] = []
        self.ctx = {"phase": "idle", "cap_w": None}
        self.running: set[int] = set()
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.on_sample = None  # callback(sample) for safety checks

    def now(self) -> float:
        return time.monotonic() - self.t0

    def snapshot(self) -> dict[int, dict]:
        out = run(["nvidia-smi", f"--query-gpu={SMI_FIELDS}", "--format=csv,noheader,nounits"])
        res = {}
        for line in out.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            idx = int(f[0])
            if idx not in self.gpus:
                continue
            res[idx] = {
                "core_c": num(f[1]), "hbm_c": num(f[2]), "sm_mhz": num(f[3]),
                "power_w": num(f[4]), "plimit_w": num(f[5]), "util_pct": num(f[6]),
                "sw_power_cap": int(f[7] == "Active"), "sw_thermal": int(f[8] == "Active"),
                "hw_thermal": int(f[9] == "Active"), "hw_slowdown": int(f[10] == "Active"),
            }
        return res

    def run(self):
        while not self.stop_ev.is_set():
            t = self.now()
            try:
                snap = self.snapshot()
            except Exception as e:  # noqa: BLE001 - keep sampling through a hiccup
                log(f"sample failed: {e}")
                snap = {}
            systin = self.fans.systin_c()
            with self.lock:
                for g, s in snap.items():
                    rpm, duty = self.fans.read(g)
                    s.update(t_s=round(t, 1), utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                             gpu=g, running=int(g in self.running), fan_rpm=rpm, duty_pct=duty,
                             systin_c=systin, **self.ctx)
                    self.samples.append(s)
                    if self.on_sample:
                        self.on_sample(s)
            self.stop_ev.wait(self.interval)

    def window(self, gpu: int, t_from: float, t_to: float) -> list[dict]:
        with self.lock:
            return [s for s in self.samples if s["gpu"] == gpu and t_from <= s["t_s"] <= t_to]


def hot(s: dict):
    return s["hbm_c"] if s.get("hbm_c") is not None else s.get("core_c")


def stats(win: list[dict]) -> dict:
    """Peaks + fan over the whole run window; power/clock/throttle over the
    under-load samples only (so model-load seconds don't dilute them); "steady" =
    mean of the last half of the under-load samples."""
    if not win:
        return {}
    act = [r for r in win if (r.get("util_pct") or 0) >= 50] or win
    half = act[len(act) // 2:]

    def vals(rows, k):
        return [r[k] for r in rows if r.get(k) is not None]

    def mean(rows, k, nd=1):
        v = vals(rows, k)
        return round(statistics.fmean(v), nd) if v else None

    clk = [c for c in vals(act, "sm_mhz") if c > 0]
    pct = lambda k: round(100 * sum(r[k] for r in act) / len(act))  # noqa: E731
    rpm = vals(win, "fan_rpm")
    return {
        "peak_hbm_c": max(vals(win, "hbm_c"), default=None),
        "peak_core_c": max(vals(win, "core_c"), default=None),
        "steady_hbm_c": mean(half, "hbm_c"), "steady_core_c": mean(half, "core_c"),
        "mean_power_w": mean(act, "power_w"),
        "min_sm_mhz": min(clk, default=None),
        "median_sm_mhz": statistics.median(clk) if clk else None,
        "fan_rpm_max": max(rpm, default=None),
        "fan_rpm_mean": round(statistics.fmean(rpm)) if rpm else None,
        "duty_max_pct": max(vals(win, "duty_pct"), default=None),
        "pct_sw_power_cap": pct("sw_power_cap"),
        "pct_thermal_slowdown": round(100 * sum(r["sw_thermal"] or r["hw_thermal"] for r in act) / len(act)),
        "pct_hw_slowdown": pct("hw_slowdown"),
        "systin_mean_c": mean(win, "systin_c"),
    }


# ------------------------------------------------------------- llama-swap
def http(method: str, path: str, timeout: float = 10):
    req = urllib.request.Request(LLAMA_SWAP + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def current_mode() -> str | None:
    try:
        return run(["python3", MODE_TOOL, "current"]).strip() or None
    except Exception:  # noqa: BLE001
        return None


def llama_swap_occupants(gpus: list[int]) -> list[str]:
    """Running llama-swap models pinned (per the active mode) to any target GPU."""
    mode = current_mode()
    if not mode:
        return []
    eff = json.loads(run(["python3", MODE_TOOL, "--json", "show", mode]))["effective"]
    running = json.loads(http("GET", "/running"))["running"]
    out = []
    for r in running:
        pin = str(eff.get(r["model"], {}).get("gpus", ""))
        if {int(x) for x in pin.split(",") if x.strip().isdigit()} & set(gpus):
            out.append(r["model"])
    return out


def gpu_state(gpus: list[int]) -> dict[int, dict]:
    out = run(["nvidia-smi", "--query-gpu=index,name,memory.used,utilization.gpu,power.min_limit,"
               "power.max_limit,pci.bus_id", "--format=csv,noheader,nounits"])
    res = {}
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if int(f[0]) in gpus:
            res[int(f[0])] = {"name": f[1], "mem_mb": num(f[2]), "util": num(f[3]),
                              "pmin": num(f[4]), "pmax": num(f[5]), "bus": f[6]}
    return res


def compute_apps(bus: str) -> str:
    try:
        out = run(["nvidia-smi", "--query-compute-apps=gpu_bus_id,pid,process_name,used_memory",
                   "--format=csv,noheader"])
    except Exception:  # noqa: BLE001
        return "?"
    return "; ".join(l.split(",", 1)[1].strip() for l in out.strip().splitlines()
                     if l.split(",")[0].strip().lower() == bus.lower()) or "(no compute apps listed)"


# ------------------------------------------------------------- benchmark
class Bench:
    def __init__(self, a, sampler: Sampler):
        self.a, self.sampler = a, sampler
        self.procs: dict[int, subprocess.Popen] = {}
        self.killed: dict[int, str] = {}
        self.stopping = False
        self.plock = threading.Lock()

    def env(self, gpu: int) -> dict:
        e = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=str(gpu))
        return e

    def once(self, gpu: int, reps: int) -> tuple[float | None, float | None, str]:
        cmd = [self.a.bin, "-m", self.a.model, "-ngl", str(self.a.ngl), "-p", str(self.a.pp),
               "-n", str(self.a.ng), "-r", str(reps), "-o", "jsonl", *shlex.split(self.a.bench_args)]
        with self.plock:
            if self.stopping:
                return None, None, "interrupted"
            if gpu in self.killed:
                return None, None, self.killed[gpu]
            p = subprocess.Popen(cmd, env=self.env(gpu), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
            self.procs[gpu] = p
        out, err = p.communicate()
        with self.plock:
            self.procs.pop(gpu, None)
        pp = tg = None
        for line in out.splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("n_prompt", 0) > 0 and j.get("n_gen", 0) == 0:
                pp = j.get("avg_ts")
            elif j.get("n_gen", 0) > 0 and j.get("n_prompt", 0) == 0:
                tg = j.get("avg_ts")
        if gpu in self.killed:
            return pp, tg, self.killed[gpu]
        if p.returncode != 0 or (pp is None and tg is None):
            tail = " | ".join([l for l in err.splitlines() if re.search(r"error|fail|memory", l, re.I)][-2:])
            return pp, tg, f"bench failed rc={p.returncode}: {tail[:200]}"
        return pp, tg, "ok"

    def kill(self, gpu: int, why: str) -> None:
        with self.plock:
            if gpu not in self.killed:
                self.killed[gpu] = why
                log(f"GPU{gpu}: {why} — stopping its benchmark")
            p = self.procs.get(gpu)
            if p and p.poll() is None:
                p.terminate()

    def kill_all(self) -> None:
        with self.plock:
            self.stopping = True
            for p in self.procs.values():
                if p.poll() is None:
                    p.terminate()

    def phase(self, gpus: list[int], phase: str, duration: float) -> dict[int, dict]:
        """Run one phase on all `gpus` concurrently. duration=0 -> one llama-bench
        with --reps; >0 -> back-to-back --soak-reps runs until the time is up."""
        self.killed.clear()
        res: dict[int, dict] = {}

        def worker(g):
            t_start = self.sampler.now()
            with self.sampler.lock:
                self.sampler.running.add(g)
            pps, tgs, status = [], [], "ok"
            try:
                deadline = time.monotonic() + duration
                while True:
                    pp, tg, status = self.once(g, self.a.reps if not duration else self.a.soak_reps)
                    if pp is not None:
                        pps.append(pp)
                    if tg is not None:
                        tgs.append(tg)
                    if status != "ok" or not duration or time.monotonic() >= deadline:
                        break
            finally:
                with self.sampler.lock:
                    self.sampler.running.discard(g)
            t_end = self.sampler.now()
            # Soak: drop the first (cold) iteration when there are more.
            pick = lambda v: round(statistics.median(v[1:] if len(v) > 1 else v), 2) if v else None  # noqa: E731
            res[g] = {"t_start": t_start, "t_end": t_end, "pp_ts": pick(pps), "tg_ts": pick(tgs),
                      "iterations": max(len(pps), len(tgs)), "status": status}

        ths = [threading.Thread(target=worker, args=(g,)) for g in gpus]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        return res


def cooldown(sampler: Sampler, gpus: list[int], cool_to: float, cool_max: float) -> dict[int, float | None]:
    """Wait until every GPU's hot metric <= cool_to (max cool_max s). Returns per-GPU
    seconds to get there (None = didn't within cool_max)."""
    t0 = sampler.now()
    done: dict[int, float | None] = {}
    while True:
        latest = {}
        with sampler.lock:
            for s in reversed(sampler.samples):
                if s["gpu"] in gpus and s["gpu"] not in latest and s["t_s"] >= t0:
                    latest[s["gpu"]] = s
        for g, s in latest.items():
            if g not in done and hot(s) is not None and hot(s) <= cool_to:
                done[g] = round(s["t_s"] - t0, 1)
        if len(done) == len(gpus) or sampler.now() - t0 >= cool_max:
            return {g: done.get(g) for g in gpus}
        time.sleep(1)


# ------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gpus", type=int, nargs="+", default=[1], help="GPU indices (nvidia-smi/PCI order; default 1)")
    ap.add_argument("--caps", type=int, nargs="+", default=[250, 200, 175, 150], help="caps in W (default 250 200 175 150)")
    ap.add_argument("--sequential", action="store_true", help="bench one GPU at a time instead of lockstep")
    ap.add_argument("--model", default=next((m for m in MODEL_CANDIDATES if os.path.isfile(m)), None),
                    help="GGUF to bench (default: current coding Qwen3.8-27B Q6_K, else Qwen3.6-27B Q6_K)")
    ap.add_argument("--bin", default=DEFAULT_BIN, help="llama-bench binary")
    ap.add_argument("--ngl", type=int, default=999, help="GPU layers (lower for a tight-fit card)")
    ap.add_argument("--pp", type=int, default=2048, help="prefill tokens (default 2048)")
    ap.add_argument("--ng", type=int, default=512, help="decode tokens (default 512)")
    ap.add_argument("--reps", type=int, default=3, help="llama-bench repetitions per cap (default 3)")
    ap.add_argument("--bench-args", default="", help="extra llama-bench args, e.g. '-fa on'")
    ap.add_argument("--soak", type=int, default=0, help="extra steady-state phase: seconds of back-to-back runs per cap")
    ap.add_argument("--soak-reps", type=int, default=5, help="llama-bench repetitions per soak iteration (default 5)")
    ap.add_argument("--no-bench", action="store_true", help="with --soak: skip the short bench phase")
    ap.add_argument("--interval", type=float, default=2.0, help="telemetry sample period s (default 2)")
    ap.add_argument("--cool-to", type=float, default=55, help="cool HBM (core if no HBM sensor) to this C (default 55)")
    ap.add_argument("--cool-max", type=float, default=180, help="max cooldown wait s (default 180)")
    ap.add_argument("--throttle-c", type=float, default=85, help="flag runs whose peak HBM reaches this (default 85)")
    ap.add_argument("--hbm-ceiling", type=float, default=90, help="kill a run if HBM reaches this C (default 90)")
    ap.add_argument("--max-used-mb", type=int, default=2048, help="refuse a GPU with more VRAM in use (default 2048)")
    ap.add_argument("--free", action="store_true", help="unload llama-swap models pinned to the target GPUs (re-warmed after)")
    ap.add_argument("--no-rewarm", action="store_true", help="with --free: don't re-warm the mode's models at the end")
    ap.add_argument("--force", action="store_true", help="skip the busy-GPU / VRAM-in-use refusal")
    ap.add_argument("--label", default="", help="tag for filenames + CSV rows, e.g. dual40-idx1_blower-idx2")
    ap.add_argument("--out-dir", default=DEFAULT_OUT, help=f"CSV output dir (default {DEFAULT_OUT})")
    ap.add_argument("--dry-run", action="store_true", help="print plan + preflight only; change nothing")
    a = ap.parse_args()
    gpus = sorted(set(a.gpus))
    if a.no_bench and not a.soak:
        ap.error("--no-bench needs --soak")

    # ---- preflight
    problems: list[str] = []
    if not a.model or not os.path.isfile(a.model):
        problems.append(f"model not found: {a.model}")
    if not os.access(a.bin, os.X_OK):
        problems.append(f"llama-bench not found: {a.bin}")
    cfg = load_config()
    pl = cfg.get("power_limits", {})
    if subprocess.run(["systemctl", "is-active", "--quiet", FAN_SERVICE]).returncode != 0:
        problems.append(f"{FAN_SERVICE} is not active — it is what applies the caps (and runs the fans!)")
    state = gpu_state(gpus)
    fans = Fans(cfg, gpus)
    occupants = []
    try:
        occupants = llama_swap_occupants(gpus)
    except Exception as e:  # noqa: BLE001
        log(f"(couldn't query llama-swap: {e})")
    for g in gpus:
        s = state.get(g)
        if not s:
            problems.append(f"GPU{g} not present in nvidia-smi")
            continue
        if str(g) not in pl:
            problems.append(f"GPU{g} has no power_limits entry in {FAN_CONFIG}")
        bad = [c for c in a.caps if s["pmin"] and s["pmax"] and not (s["pmin"] <= c <= s["pmax"])]
        if bad:
            problems.append(f"GPU{g} caps {bad} outside board range {s['pmin']:.0f}-{s['pmax']:.0f} W")
        if g not in fans.paths:
            log(f"note: GPU{g} has no controlled fan zone — RPM/duty columns will be empty")
    print(f"\nPlan: GPUs {gpus} ({'sequential' if a.sequential else 'lockstep'}), caps {a.caps} W, "
          f"model {os.path.basename(a.model or '?')}, pp{a.pp}/tg{a.ng} x{a.reps}"
          + (f", soak {a.soak}s x{a.soak_reps}" if a.soak else "") + (" (soak only)" if a.no_bench else ""))
    for g in gpus:
        s = state.get(g, {})
        print(f"  GPU{g} {s.get('name', '?')}: cap now {pl.get(str(g))} W (config) | VRAM used "
              f"{s.get('mem_mb')} MiB | util {s.get('util')}% | fan {fans.describe(g)}")
    print(f"  llama-swap models on these GPUs: {occupants or 'none'}"
          + (" -> will unload (--free)" if occupants and a.free else ""))
    if problems:
        print("\nCannot run:\n  - " + "\n  - ".join(problems))
        return 2
    if a.dry_run:
        print("\n(dry run — nothing changed)")
        return 0

    # ---- free the cards (only what's pinned to them), then re-check
    freed: list[str] = []
    if a.free and occupants:
        for m in occupants:
            log(f"unloading llama-swap model '{m}'")
            http("POST", f"/api/models/unload/{m}", timeout=60)
            freed.append(m)
        for _ in range(60):
            state = gpu_state(gpus)
            if all((state[g]["mem_mb"] or 0) <= a.max_used_mb for g in gpus):
                break
            time.sleep(1)
    state = gpu_state(gpus)
    busy = [g for g in gpus if (state[g]["mem_mb"] or 0) > a.max_used_mb or (state[g]["util"] or 0) > 10]
    if busy and not a.force:
        for g in busy:
            print(f"GPU{g} busy: {state[g]['mem_mb']:.0f} MiB used, util {state[g]['util']:.0f}% — "
                  f"{compute_apps(state[g]['bus'])}")
        print("Refusing to disturb running work (free it, e.g. --free / stop ComfyUI, or --force).")
        if freed and not a.no_rewarm:
            rewarm()
        return 3

    # ---- run
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = os.path.join(a.out_dir, f"power-cap-sweep-{stamp}" + (f"-{a.label}" if a.label else ""))
    os.makedirs(a.out_dir, exist_ok=True)
    orig = {g: int(pl[str(g)]) for g in gpus}
    recheck = float(cfg.get("power_recheck_sec", 30))
    sampler = Sampler(gpus, fans, a.interval)
    bench = Bench(a, sampler)

    def safety(s):
        if s["running"] and s.get("hbm_c") is not None and s["hbm_c"] >= a.hbm_ceiling:
            threading.Thread(target=bench.kill, args=(s["gpu"], f"HBM {s['hbm_c']:.0f}C >= ceiling {a.hbm_ceiling:.0f}C"),
                             daemon=True).start()
    sampler.on_sample = safety

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    rows: list[dict] = []
    groups = [[g] for g in gpus] if a.sequential else [gpus]
    phases = ([] if a.no_bench else [("bench", 0)]) + ([("soak", a.soak)] if a.soak else [])
    print(f"\n{'cap':>4} {'gpu':>3} {'phase':<5} | {'pp t/s':>8} {'tg t/s':>7} | {'pkHBM':>5} {'pkCore':>6} "
          f"{'stHBM':>5} {'avgW':>5} {'minClk':>6} | {'fanRPM':>6} {'duty':>4} | {'pwrcap%':>7} {'therm%':>6} | cool_s")
    rc = 0
    sampler.start()
    try:
        for cap in a.caps:
            for group in groups:
                targets = {g: cap for g in group}
                sampler.ctx = {"phase": "setcap", "cap_w": cap}
                set_caps(targets)
                if not wait_enforced(targets, recheck + 30):
                    raise RuntimeError(f"cap {cap} W not enforced within {recheck + 30:.0f}s on {group} "
                                       f"(live {live_limits()}) — is {FAN_SERVICE} healthy?")
                for phase, dur in phases:
                    sampler.ctx = {"phase": "precool", "cap_w": cap}
                    cooldown(sampler, group, a.cool_to, a.cool_max)
                    utc_start = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                    sampler.ctx = {"phase": phase, "cap_w": cap}
                    res = bench.phase(group, phase, dur)
                    sampler.ctx = {"phase": "cool", "cap_w": cap}
                    cool = cooldown(sampler, group, a.cool_to, a.cool_max)
                    for g in group:
                        r = res[g]
                        st = stats(sampler.window(g, r["t_start"], r["t_end"]))
                        row = {"utc_start": utc_start, "label": a.label,
                               "mode": "sequential" if a.sequential else "lockstep", "phase": phase,
                               "gpu": g, "cap_w": cap, "model": os.path.basename(a.model), "pp": a.pp,
                               "ng": a.ng, "reps": a.soak_reps if dur else a.reps, "pp_ts": r["pp_ts"],
                               "tg_ts": r["tg_ts"], "iterations": r["iterations"],
                               "duration_s": round(r["t_end"] - r["t_start"]), **st,
                               "cool_s": cool.get(g), "status": r["status"]}
                        rows.append(row)
                        flag = " <-THROTTLE" if (row.get("peak_hbm_c") or 0) >= a.throttle_c else ""
                        f = lambda v, w, p=0: f"{v:>{w}.{p}f}" if isinstance(v, (int, float)) else f"{'-':>{w}}"  # noqa: E731
                        print(f"{cap:>4} {g:>3} {phase:<5} | {f(row['pp_ts'], 8, 1)} {f(row['tg_ts'], 7, 1)} | "
                              f"{f(row.get('peak_hbm_c'), 5)} {f(row.get('peak_core_c'), 6)} "
                              f"{f(row.get('steady_hbm_c'), 5, 1)} {f(row.get('mean_power_w'), 5)} "
                              f"{f(row.get('min_sm_mhz'), 6)} | {f(row.get('fan_rpm_max'), 6)} "
                              f"{f(row.get('duty_max_pct'), 4)} | {f(row.get('pct_sw_power_cap'), 7)} "
                              f"{f(row.get('pct_thermal_slowdown'), 6)} | "
                              f"{row['cool_s'] if row['cool_s'] is not None else '>' + str(int(a.cool_max))}"
                              f"{flag}{'' if r['status'] == 'ok' else '  [' + r['status'] + ']'}", flush=True)
    except (KeyboardInterrupt, SystemExit) as e:
        log("interrupted — cleaning up")
        rc = e.code if isinstance(e, SystemExit) and isinstance(e.code, int) else 130
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
        rc = 1
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)  # a second Ctrl-C must not skip the cap restore
        bench.kill_all()
        sampler.stop_ev.set()
        sampler.join(timeout=10)
        try:
            set_caps(orig)
            ok = wait_enforced(orig, recheck + 30)
            log(f"restored caps {orig} W" + ("" if ok else " in config (daemon not yet applied — check!)"))
        except Exception as e:  # noqa: BLE001
            log(f"WARNING: cap restore failed ({e}) — set power_limits {orig} in {FAN_CONFIG} by hand!")
            rc = rc or 1
        if rows:
            with open(base + ".csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=SUMMARY_COLS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
        if sampler.samples:
            with open(base + "-samples.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=SAMPLE_COLS, extrasaction="ignore")
                w.writeheader()
                w.writerows(sampler.samples)
            log(f"wrote {base}.csv (+ -samples.csv)" if rows else f"wrote {base}-samples.csv")
        if freed and not a.no_rewarm:
            rewarm()
    return rc


def rewarm() -> None:
    mode = current_mode()
    if mode:
        log(f"re-warming '{mode}' mode models (llama-swap-mode.py set {mode})")
        subprocess.run(["python3", MODE_TOOL, "set", mode], check=False)


if __name__ == "__main__":
    sys.exit(main())

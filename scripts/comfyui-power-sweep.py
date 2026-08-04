#!/usr/bin/env python3
"""
comfyui-power-sweep.py — measure whether ComfyUI generation on the V100 benefits
from a higher GPU power cap.

For each power cap (default 175, 200, 250 W) it:
  1. Writes the cap into gpu-fan-control.config.json for the target GPU and
     restarts the fan daemon, so (a) the cap is actually enforced and (b) the
     fans keep tracking temperature (essential when testing 250 W, where the
     V100 HBM runs hot). The daemon otherwise re-reverts any manual `nvidia-smi
     -pl` back to 175 W every 30 s, which would corrupt the measurement.
  2. Submits a workflow to the ALREADY-RUNNING comfyui-open instance (idx1),
     one warm/priming run (discarded) then N timed runs.
  3. Samples power.draw / HBM temp / core temp / SM clock / SM util once a
     second during each run and records peak/mean.

It reports, per cap: warm avg & best wall time, peak HBM temp, peak power,
min SM clock, mean SM util — so you can see if more watts buy speed or just
heat/throttle. Restores the original 175 W caps + rewarms the daily llama-swap
models on exit.

Runs the V100 clean: unloads all llama-swap models first and keeps them
unloaded for the duration (a background unloader), then restores `daily`.

REQUIRES ROOT (restarts gpu-fan-control, which applies the caps):
    sudo /srv/ai/venvs/comfyui/bin/python scripts/comfyui-power-sweep.py --label t2i
    sudo /srv/ai/venvs/comfyui/bin/python scripts/comfyui-power-sweep.py \
        --label video --workflow /path/to/exported_video_api.json --runs 2

Export a video workflow from the ComfyUI UI via the API format
(Workflow -> Export (API)) and pass it with --workflow.
"""
import argparse
import csv
import json
import os
import pwd
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

FAN_CONFIG = "/srv/ai/scripts/gpu-fan-control.config.json"
FAN_SERVICE = "gpu-fan-control"
SWAP = "http://127.0.0.1:9090"
RESULTS_DIR = "/srv/ai/benchmarks/comfyui-power-sweep"
ENV = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}

# Built-in SDXL text-to-image workflow (self-contained, no input assets).
T2I_PROMPT = ("a highly detailed photograph of a red fox sitting in a snowy pine "
              "forest at golden hour, sharp focus, bokeh, national geographic")
T2I_NEG = "blurry, low quality, watermark, text, deformed"


def build_t2i_workflow(seed, ckpt="sd_xl_base_1.0.safetensors",
                       width=1024, height=1024, steps=30):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": T2I_PROMPT, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": T2I_NEG, "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 7.0,
                         "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                         "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "pwrsweep", "images": ["8", 0]}},
    }


# ---------------------------------------------------------------- HTTP helpers
def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def http_post(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def comfy_ready(port, timeout=5):
    try:
        s, _ = http_get(f"http://127.0.0.1:{port}/system_stats", timeout=timeout)
        return s == 200
    except Exception:
        return False


def randomize_seeds(wf):
    """Bump any KSampler/*Sampler* 'seed'/'noise_seed' so each run isn't cache-hit."""
    import random
    s = random.randint(1, 2**31)
    for node in wf.values():
        ins = node.get("inputs", {})
        for k in ("seed", "noise_seed"):
            if k in ins and isinstance(ins[k], int):
                ins[k] = s
    return wf


def run_once(port, wf, hard_timeout=1800):
    """Submit workflow, block until it completes, return wall seconds (or -1)."""
    t0 = time.time()
    try:
        _, resp = http_post(f"http://127.0.0.1:{port}/prompt", {"prompt": wf}, timeout=30)
    except Exception as e:
        print(f"    submit failed: {e}")
        return -1.0
    pid = resp.get("prompt_id")
    if not pid:
        print(f"    no prompt_id in response: {resp}")
        return -1.0
    while True:
        try:
            _, body = http_get(f"http://127.0.0.1:{port}/history/{pid}", timeout=10)
            hist = json.loads(body)
            h = hist.get(pid)
            if h:
                st = h.get("status", {})
                if st.get("completed") or h.get("outputs"):
                    if st.get("status_str") == "error":
                        print("    workflow errored (see comfyui-open logs)")
                        return -1.0
                    return time.time() - t0
        except Exception:
            pass
        if time.time() - t0 > hard_timeout:
            print(f"    timed out after {hard_timeout}s")
            return -1.0
        time.sleep(0.25)


# ------------------------------------------------------------ llama-swap clean
def swap_unload_all():
    for path in ("/unload", "/api/unload"):
        try:
            subprocess.run(["curl", "-s", "-m", "10", SWAP + path],
                           capture_output=True, timeout=15)
        except Exception:
            pass


_keepalive = {"run": False}


def keepalive_unloader():
    while _keepalive["run"]:
        swap_unload_all()
        time.sleep(4)


# --------------------------------------------------------------- GPU telemetry
def gpu_sample(idx):
    """Return (power_W, hbm_C, core_C, sm_MHz, sm_pct) or Nones."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "-i", str(idx),
             "--query-gpu=power.draw,temperature.memory,temperature.gpu,clocks.sm,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, env=ENV)
        parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
        out = []
        for p in parts:
            try:
                out.append(float(p))
            except ValueError:
                out.append(None)
        while len(out) < 5:
            out.append(None)
        return tuple(out[:5])
    except Exception:
        return (None, None, None, None, None)


class Telemetry(threading.Thread):
    def __init__(self, idx, hbm_ceiling):
        super().__init__(daemon=True)
        self.idx = idx
        self.hbm_ceiling = hbm_ceiling
        self.run_flag = True
        self.pw, self.hbm, self.core, self.clk, self.util = [], [], [], [], []
        self.over_ceiling = False

    def run(self):
        while self.run_flag:
            pw, hbm, core, clk, util = gpu_sample(self.idx)
            if pw is not None:
                self.pw.append(pw)
            if hbm is not None:
                self.hbm.append(hbm)
                if hbm >= self.hbm_ceiling:
                    self.over_ceiling = True
            if core is not None:
                self.core.append(core)
            if clk is not None:
                self.clk.append(clk)
            if util is not None:
                self.util.append(util)
            time.sleep(1)

    def stop(self):
        self.run_flag = False
        self.join(timeout=3)

    def summary(self):
        def mx(x):
            return max(x) if x else float("nan")

        def mn(x):
            return min(x) if x else float("nan")

        def mean(x):
            return statistics.mean(x) if x else float("nan")
        return {
            "peak_hbm_c": mx(self.hbm),
            "peak_core_c": mx(self.core),
            "peak_power_w": mx(self.pw),
            "mean_power_w": mean(self.pw),
            "min_sm_mhz": mn(self.clk),
            "mean_sm_pct": mean(self.util),
        }


# --------------------------------------------------------------- power capping
def _restore_owner(path, ref_stat):
    try:
        os.chmod(path, ref_stat.st_mode)
        os.chown(path, ref_stat.st_uid, ref_stat.st_gid)
    except Exception:
        pass


def restart_fan_daemon():
    subprocess.run(["systemctl", "restart", FAN_SERVICE], check=False)


def enforced_pl(idx):
    try:
        r = subprocess.run(["nvidia-smi", "-i", str(idx),
                            "--query-gpu=power.limit", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10, env=ENV)
        return int(round(float(r.stdout.strip().splitlines()[0])))
    except Exception:
        return -1


def set_cap_via_daemon(idx, watts, cfg_stat, timeout=25):
    """Write the cap into the fan config, restart the daemon, wait until enforced."""
    with open(FAN_CONFIG) as f:
        cfg = json.load(f)
    cfg.setdefault("power_limits", {})[str(idx)] = watts
    with open(FAN_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    _restore_owner(FAN_CONFIG, cfg_stat)
    restart_fan_daemon()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if enforced_pl(idx) == watts:
            return True
        time.sleep(1)
    return enforced_pl(idx) == watts


# ----------------------------------------------------------------------- sweep
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gpu", type=int, default=1,
                    help="physical GPU index to cap+measure (default 1 = comfyui-open V100)")
    ap.add_argument("--port", type=int, default=8188,
                    help="running ComfyUI port (default 8188 = comfyui-open)")
    ap.add_argument("--caps", type=int, nargs="+", default=[175, 200, 250],
                    help="power caps in watts to test (default: 175 200 250)")
    ap.add_argument("--runs", type=int, default=3, help="timed runs per cap (default 3)")
    ap.add_argument("--label", default="t2i", help="label for this sweep (csv/name)")
    ap.add_argument("--workflow", help="path to an exported API-format workflow JSON "
                    "(omit to use the built-in SDXL txt2img)")
    ap.add_argument("--ckpt", default="sd_xl_base_1.0.safetensors",
                    help="checkpoint for the built-in T2I workflow")
    ap.add_argument("--steps", type=int, default=30, help="steps for built-in T2I")
    ap.add_argument("--size", type=int, default=1024, help="W=H for built-in T2I")
    ap.add_argument("--hbm-ceiling", type=int, default=90,
                    help="abort a cap if HBM temp reaches this (C); safety (default 90)")
    ap.add_argument("--no-restore", action="store_true",
                    help="don't rewarm daily llama-swap models at the end")
    ap.add_argument("--run-timeout", type=int, default=1800,
                    help="per-generation hard timeout in seconds (default 1800)")
    a = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Must run as root (restarts gpu-fan-control to apply caps): use sudo.")
    if not os.path.exists(FAN_CONFIG):
        sys.exit(f"fan config not found: {FAN_CONFIG}")
    if not comfy_ready(a.port):
        sys.exit(f"ComfyUI not responding on :{a.port}. Start comfyui-open first "
                 f"(sudo systemctl start comfyui-open comfyui-secure).")

    # Load the workflow (external or built-in).
    external = None
    if a.workflow:
        with open(a.workflow) as f:
            external = json.load(f)
        # Accept either a bare API dict or a UI export with a 'prompt' key.
        if "prompt" in external and all(isinstance(v, dict) for v in external.get("prompt", {}).values()):
            external = external["prompt"]
        print(f"Using external workflow: {a.workflow} ({len(external)} nodes)")
    else:
        print(f"Using built-in SDXL T2I: {a.ckpt} {a.size}x{a.size} {a.steps} steps")

    def make_wf():
        if external is not None:
            return randomize_seeds(json.loads(json.dumps(external)))
        return build_t2i_workflow(seed=int(time.time() * 1000) % (2**31),
                                  ckpt=a.ckpt, width=a.size, height=a.size, steps=a.steps)

    cfg_stat = os.stat(FAN_CONFIG)
    with open(FAN_CONFIG) as f:
        orig_power_limits = dict(json.load(f).get("power_limits", {}))
    orig_cap = orig_power_limits.get(str(a.gpu), 175)

    restored = {"done": False}

    def restore_all():
        if restored["done"]:
            return
        restored["done"] = True
        print(f"\n[restore] returning GPU{a.gpu} cap to {orig_cap} W + restarting fan daemon...")
        try:
            with open(FAN_CONFIG) as f:
                cfg = json.load(f)
            cfg["power_limits"] = orig_power_limits
            with open(FAN_CONFIG, "w") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")
            _restore_owner(FAN_CONFIG, cfg_stat)
            restart_fan_daemon()
        except Exception as e:
            print(f"    WARN: cap restore failed: {e} — check {FAN_CONFIG} manually!")
        _keepalive["run"] = False
        time.sleep(1)
        if not a.no_restore:
            print("[restore] rewarming daily llama-swap models...")
            subprocess.run(["python3", "/srv/ai/scripts/llama-swap-mode.py", "set", "daily"],
                           capture_output=True)

    def on_signal(signum, _frame):
        print(f"\n[signal {signum}] restoring...")
        restore_all()
        sys.exit(1)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    rows = []
    try:
        print("Unloading all llama-swap models for a clean card...")
        swap_unload_all()
        time.sleep(3)
        _keepalive["run"] = True
        threading.Thread(target=keepalive_unloader, daemon=True).start()

        for cap in a.caps:
            print(f"\n{'='*70}\n### cap = {cap} W  (GPU{a.gpu})")
            if not set_cap_via_daemon(a.gpu, cap, cfg_stat):
                print(f"  FAILED to enforce {cap} W (enforced={enforced_pl(a.gpu)} W); skipping.")
                continue
            print(f"  enforced power limit: {enforced_pl(a.gpu)} W")

            # Warm/prime run (checkpoint load; discarded).
            print("  priming (discarded)...", flush=True)
            prime = run_once(a.port, make_wf(), hard_timeout=a.run_timeout)
            if prime < 0:
                print("  prime failed; skipping this cap.")
                continue

            times, tel_summaries = [], []
            aborted = False
            for i in range(a.runs):
                tel = Telemetry(a.gpu, a.hbm_ceiling)
                tel.start()
                dt = run_once(a.port, make_wf(), hard_timeout=a.run_timeout)
                tel.stop()
                s = tel.summary()
                if dt > 0:
                    times.append(dt)
                    tel_summaries.append(s)
                    print(f"    run {i+1}: {dt:6.2f}s | HBM {s['peak_hbm_c']:.0f}C "
                          f"peakW {s['peak_power_w']:.0f} minClk {s['min_sm_mhz']:.0f}MHz "
                          f"SM {s['mean_sm_pct']:.0f}%", flush=True)
                else:
                    print(f"    run {i+1}: FAILED")
                if tel.over_ceiling:
                    print(f"    !! HBM hit {a.hbm_ceiling}C ceiling — aborting this cap for safety")
                    aborted = True
                    break
                time.sleep(1)

            if times:
                avg = statistics.mean(times)
                best = min(times)
                peak_hbm = max(s["peak_hbm_c"] for s in tel_summaries)
                peak_core = max(s["peak_core_c"] for s in tel_summaries)
                peak_w = max(s["peak_power_w"] for s in tel_summaries)
                min_clk = min(s["min_sm_mhz"] for s in tel_summaries)
                mean_sm = statistics.mean([s["mean_sm_pct"] for s in tel_summaries])
                rows.append({
                    "label": a.label, "cap_w": cap, "runs": len(times),
                    "cold_s": round(prime, 2), "warm_avg_s": round(avg, 2),
                    "warm_best_s": round(best, 2),
                    "peak_hbm_c": round(peak_hbm), "peak_core_c": round(peak_core),
                    "peak_power_w": round(peak_w), "min_sm_mhz": round(min_clk),
                    "mean_sm_pct": round(mean_sm), "aborted": int(aborted),
                })
    finally:
        restore_all()

    # ------------------------------------------------------------- report
    if not rows:
        print("\nNo results collected.")
        return
    print(f"\n{'='*78}\n### Power sweep — {a.label}  (GPU{a.gpu})")
    print(f"{'capW':>5} {'avg s':>8} {'best s':>8} {'peakHBM':>8} {'peakW':>7} "
          f"{'minClk':>8} {'SM%':>5} {'':>7}")
    base = rows[0]["warm_avg_s"]
    for r in rows:
        speed = base / r["warm_avg_s"] if r["warm_avg_s"] else 0
        flag = " ABORTED" if r["aborted"] else (f" {speed:.2f}x" if r is not rows[0] else " (base)")
        print(f"{r['cap_w']:>5} {r['warm_avg_s']:>8.2f} {r['warm_best_s']:>8.2f} "
              f"{r['peak_hbm_c']:>7}C {r['peak_power_w']:>6}W {r['min_sm_mhz']:>6}MHz "
              f"{r['mean_sm_pct']:>4}%{flag}")
    print("\nHigher watts help only if avg-s drops meaningfully AND HBM stays < ~85C "
          "(V100 soft-throttles there). If avg-s flattens while peakHBM climbs, you're "
          "thermally/clock limited, not power limited.")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = f"{RESULTS_DIR}/{a.label}_{ts}.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    # Make the CSV owned by the invoking user, not root.
    try:
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            pw = pwd.getpwnam(sudo_user)
            os.chown(out, pw.pw_uid, pw.pw_gid)
            os.chown(RESULTS_DIR, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass
    print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()

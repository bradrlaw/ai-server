#!/usr/bin/env python3
"""
ComfyUI P100-vs-V100 text-to-image benchmark.

Launches two *dedicated* temporary ComfyUI instances (one pinned to the P100
idx0, one to a V100) from the shared venv/checkpoints, submits an identical
txt2img workflow to each, and compares end-to-end wall time + the KSampler's
own it/s (parsed from each instance's log). Runs each config warm (one discarded
priming run so the checkpoint is resident) then N timed runs.

Isolation: unload all llama-swap models first so both cards are clean; each temp
instance gets its own port + temp/output/user dirs + sqlite db. Restores daily
llama-swap models on exit.

Usage (comfyui venv python):
  /srv/ai/venvs/comfyui/bin/python scripts/comfyui-gpu-bench.py
"""
import argparse, json, os, re, signal, subprocess, sys, threading, time, urllib.request, urllib.error
from datetime import datetime

VENV_PY = "/srv/ai/venvs/comfyui/bin/python"
COMFY_MAIN = "/srv/ai/comfyui/main.py"
COMFY_DIR = "/srv/ai/comfyui"
SWAP = "http://127.0.0.1:9090"
RESULTS_DIR = "/srv/ai/benchmarks/llm-scaling-bench/results"

PROMPT = ("a highly detailed photograph of a red fox sitting in a snowy pine "
          "forest at golden hour, sharp focus, bokeh, national geographic")
NEG = "blurry, low quality, watermark, text, deformed"

# GPUs to compare: label -> physical index (CUDA_DEVICE_ORDER=PCI_BUS_ID)
TARGETS = [("P100", 0, 8199), ("V100", 1, 8198)]

# (config label, checkpoint, width, height, steps)
CONFIGS = [
    ("SD1.5-512x512-30", "DreamShaper_8_pruned.safetensors", 512, 512, 30),
    ("SDXL-1024x1024-30", "sd_xl_base_1.0.safetensors", 1024, 1024, 30),
]


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def http_post(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def build_workflow(ckpt, width, height, steps, seed):
    """Core-node SD txt2img graph in ComfyUI API format."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 7.0,
                         "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                         "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "bench", "images": ["8", 0]}},
    }


def swap_unload_all():
    for path in ("/unload", "/api/unload"):
        try:
            subprocess.run(["curl", "-s", "-m", "10", SWAP + path], capture_output=True, timeout=15)
        except Exception:
            pass


_keepalive = {"run": False}


def keepalive_unloader():
    """Keep llama-swap from re-warming models onto the cards during the bench."""
    while _keepalive["run"]:
        swap_unload_all()
        time.sleep(4)


def vram_used(gpu):
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                            "--format=csv,noheader,nounits", "-i", str(gpu)],
                           capture_output=True, text=True, timeout=10,
                           env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return -1


def launch(label, gpu, port):
    tmp = f"/tmp/comfy-bench-{label}"
    for _sub in ("output", "temp", "user"):
        os.makedirs(f"{tmp}/{_sub}", exist_ok=True)
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(gpu)}
    cmd = [VENV_PY, COMFY_MAIN, "--listen", "127.0.0.1", "--port", str(port),
           "--output-directory", f"{tmp}/output", "--temp-directory", f"{tmp}/temp",
           "--user-directory", f"{tmp}/user",
           "--database-url", f"sqlite:///{tmp}/comfy.db"]
    log = open(f"/tmp/comfy-bench-{label}.log", "w")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=COMFY_DIR,
                         env=env, preexec_fn=os.setsid)
    return p


def kill(p):
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
        for _ in range(30):
            if p.poll() is not None:
                break
            time.sleep(0.5)
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass


def wait_ready(port, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s, _ = http_get(f"http://127.0.0.1:{port}/system_stats", timeout=5)
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def run_once(port, wf):
    """Submit workflow, wait for completion via /history, return wall seconds."""
    t0 = time.time()
    _, resp = http_post(f"http://127.0.0.1:{port}/prompt", {"prompt": wf}, timeout=30)
    pid = resp["prompt_id"]
    while True:
        try:
            _, body = http_get(f"http://127.0.0.1:{port}/history/{pid}", timeout=10)
            hist = json.loads(body)
            if pid in hist and hist[pid].get("status", {}).get("completed", False):
                return time.time() - t0
            if pid in hist and hist[pid].get("outputs"):
                return time.time() - t0
        except Exception:
            pass
        if time.time() - t0 > 600:
            return -1.0
        time.sleep(0.2)


# tqdm prints either "N.NNit/s" (fast) or "N.NNs/it" (slow, <1 it/s); take
# whichever token appears LAST in the (accumulated) log and normalise to it/s.
RATE_RE = re.compile(r"(\d+\.\d+)(it/s|s/it)")


def last_rate(label):
    """Parse the most recent KSampler rate (it/s) from the instance log."""
    try:
        txt = open(f"/tmp/comfy-bench-{label}.log", errors="ignore").read()
    except Exception:
        return None
    m = RATE_RE.findall(txt)
    if not m:
        return None
    val, unit = m[-1]
    val = float(val)
    if unit == "s/it":
        return 1.0 / val if val else None
    return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--no-restore", action="store_true")
    a = ap.parse_args()

    print("Unloading all llama-swap models for clean GPUs...")
    swap_unload_all()
    time.sleep(4)
    _keepalive["run"] = True
    ka = threading.Thread(target=keepalive_unloader, daemon=True)
    ka.start()

    procs = {}
    rows = []
    try:
        for label, gpu, port in TARGETS:
            print(f"Launching ComfyUI [{label}] on idx{gpu} :{port} ...", flush=True)
            procs[label] = launch(label, gpu, port)
        for label, gpu, port in TARGETS:
            if not wait_ready(port):
                print(f"[{label}] failed to start; tail:")
                print(subprocess.run(["tail", "-5", f"/tmp/comfy-bench-{label}.log"],
                                     capture_output=True, text=True).stdout)
                continue
            print(f"[{label}] ready.", flush=True)

        for cfg_label, ckpt, w, h, steps in CONFIGS:
            print(f"\n{'='*66}\n### {cfg_label}", flush=True)
            for label, gpu, port in TARGETS:
                if procs[label].poll() is not None:
                    continue
                # prime (checkpoint load, discarded)
                wf = build_workflow(ckpt, w, h, steps, seed=1)
                prime = run_once(port, wf)
                times = []
                for i in range(a.runs):
                    wf = build_workflow(ckpt, w, h, steps, seed=100 + i)
                    dt = run_once(port, wf)
                    if dt > 0:
                        times.append(dt)
                    time.sleep(0.3)
                rate = last_rate(label)
                peak = vram_used(gpu)
                avg = sum(times) / len(times) if times else -1
                best = min(times) if times else -1
                itps = rate if rate else 0
                rows.append({"config": cfg_label, "gpu": label, "cold_s": round(prime, 2),
                             "warm_avg_s": round(avg, 2), "warm_best_s": round(best, 2),
                             "sampler_it_s": round(itps, 2), "vram_mib": peak})
                print(f"  [{label:4}] cold {prime:5.1f}s | warm avg {avg:5.2f}s "
                      f"(best {best:5.2f}s) | {itps:5.2f} it/s | VRAM {peak} MiB", flush=True)
    finally:
        _keepalive["run"] = False
        time.sleep(1)
        for label, p in procs.items():
            kill(p)
        if not a.no_restore:
            print("\n[restore] rewarming daily llama-swap models...")
            subprocess.run(["python3", "/srv/ai/scripts/llama-swap-mode.py", "set", "daily"],
                           capture_output=True)

    # summary + CSV
    if rows:
        print(f"\n{'='*66}\n### Summary — P100 vs V100 txt2img")
        for cfg_label, *_ in CONFIGS:
            crows = [r for r in rows if r["config"] == cfg_label]
            if len(crows) == 2:
                by = {r["gpu"]: r for r in crows}
                if "P100" in by and "V100" in by and by["V100"]["warm_avg_s"] > 0:
                    ratio = by["P100"]["warm_avg_s"] / by["V100"]["warm_avg_s"]
                    print(f"  {cfg_label}: P100 {by['P100']['warm_avg_s']}s "
                          f"({by['P100']['sampler_it_s']} it/s) vs "
                          f"V100 {by['V100']['warm_avg_s']}s "
                          f"({by['V100']['sampler_it_s']} it/s) "
                          f"-> V100 is {ratio:.2f}x faster")
        os.makedirs(RESULTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = f"{RESULTS_DIR}/comfyui_p100_v100_{ts}.csv"
        import csv
        with open(out, "w", newline="") as f:
            wtr = csv.DictWriter(f, fieldnames=["config", "gpu", "cold_s", "warm_avg_s",
                                                "warm_best_s", "sampler_it_s", "vram_mib"])
            wtr.writeheader()
            for r in rows:
                wtr.writerow(r)
        print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()

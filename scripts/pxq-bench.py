#!/usr/bin/env python3
"""
pxq_llama (ik_llama.cpp fork) vs stock llama.cpp — single-stream latency/throughput.

For one (engine x target) combination per invocation, spawn a dedicated
llama-server pinned to the target GPU(s), run a prompt-size sweep, and record
client-side TTFT plus the server's own `timings` (exact prefill / decode rates)
and peak VRAM. Results append to a per-target CSV keyed by engine so the two
engines can be compared row-for-row on the *identical* GGUF.

Engines
  stock : /srv/ai/src/llama.cpp/build/bin/llama-server (mainline, our daily build)
  fork  : /srv/ai/src/pxq_llama/.../bin/llama-server, run PXA_ENHANCE=1
          PXA_MODE=balance (per-arch auto levers). Needs libnccl.so.2 from the
          comfyui venv on LD_LIBRARY_PATH.

Targets (model + GPU placement, identical flags for both engines)
  p100-gemma26b       gemma-4-26B-A4B Q4_K_XL MoE on idx0 (P100, sm_60)
  v100-gemma26b       same GGUF on idx1 (V100, sm_70)
  v100-qwen35moe      Qwen3.6-35B-A3B Q6_K MoE on idx1 (V100)
  dualv100-qwen35bf16 Qwen3.6-35B-A3B BF16 split idx1+idx2 (-sm layer)
  dualv100-qwen27bf16 Qwen3.6-27B BF16 split idx1+idx2 (-sm layer)

Examples
  python3 scripts/pxq-bench.py --engine stock --target p100-gemma26b
  python3 scripts/pxq-bench.py --engine fork  --target p100-gemma26b
  python3 scripts/pxq-bench.py --engine fork  --target dualv100-qwen35bf16 --spec mtp:n_max=1

stdlib only.
"""
import argparse, csv, json, os, signal, subprocess, sys, threading, time
import urllib.request, urllib.error
from datetime import datetime

MODELS_DIR = "/srv/ai/models"
SWAP = "http://127.0.0.1:9090"
DATA_DIR = "/srv/ai/docs/data/pxq"


def pick_port():
    import socket
    for p in range(10090, 10130):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    return 10099


PORT = pick_port()

STOCK_BIN = "/srv/ai/src/llama.cpp/build/bin/llama-server"
FORK_ROOT = "/srv/ai/src/pxq_llama/pxq_llama-2026-07-22-linux-x64"
FORK_BIN = f"{FORK_ROOT}/bin/llama-server"
NCCL_DIR = "/srv/ai/venvs/comfyui/lib/python3.12/site-packages/nvidia/nccl/lib"
FORK_LD = ":".join([f"{FORK_ROOT}/bin", f"{FORK_ROOT}/src", f"{FORK_ROOT}/ggml/src",
                    f"{FORK_ROOT}/examples/mtmd", "/usr/local/cuda/lib64", NCCL_DIR])

ENGINES = {
    "stock": {"bin": STOCK_BIN, "env": {}},
    "fork":  {"bin": FORK_BIN,
              "env": {"LD_LIBRARY_PATH": FORK_LD, "PXA_ENHANCE": "1", "PXA_MODE": "balance"}},
}

# gpus are PCI_BUS_ID indices (0=P100, 1/2=V100). ctx/batch/ubatch shared by both engines.
# Strategy: STANDARD-quant model on stock llama.cpp  vs  the fork's PXQ quant of the
# SAME base model (Qwen3.6-35B-A3B, a 256-expert hybrid SSM+MoE) on the fork.
Q35 = f"{MODELS_DIR}/qwen3.6-35b-a3b"
PXQ = f"{MODELS_DIR}/pxq"
TARGETS = {
    # --- stock llama.cpp on the standard quant (baseline) ---
    "v100-qwen35-q6k": {  # Q6_K ~29GB fits a single V100
        "model": f"{Q35}/Qwen3.6-35B-A3B-UD-Q6_K.gguf",
        "gpus": [1], "ctx": 8192, "batch": 2048, "ubatch": 2048},
    "v100-qwen35-q4km": {  # Q4_K_M (uncensored variant) nearest to PXQ4 size
        "model": f"{Q35}/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf",
        "gpus": [1], "ctx": 8192, "batch": 2048, "ubatch": 2048},
    "dualv100-qwen35-q6k": {  # Q6_K split across both V100s
        "model": f"{Q35}/Qwen3.6-35B-A3B-UD-Q6_K.gguf",
        "gpus": [1, 2], "ctx": 8192, "batch": 2048, "ubatch": 2048, "split": "layer"},

    # --- fork on our self-made PXQ quants of the same base model ---
    "v100-qwen35-pxq6": {"model": f"{PXQ}/Qwen3.6-35B-A3B-PXQ6.gguf",
        "gpus": [1], "ctx": 8192, "batch": 2048, "ubatch": 2048},
    "v100-qwen35-pxq4": {"model": f"{PXQ}/Qwen3.6-35B-A3B-PXQ4.gguf",
        "gpus": [1], "ctx": 8192, "batch": 2048, "ubatch": 2048},
    "v100-qwen35-pxq3": {"model": f"{PXQ}/Qwen3.6-35B-A3B-PXQ3.gguf",
        "gpus": [1], "ctx": 8192, "batch": 2048, "ubatch": 2048},
    "v100-qwen35-pxq2": {"model": f"{PXQ}/Qwen3.6-35B-A3B-PXQ2.gguf",
        "gpus": [1], "ctx": 8192, "batch": 2048, "ubatch": 2048},
    "p100-qwen35-pxq2": {"model": f"{PXQ}/Qwen3.6-35B-A3B-PXQ2.gguf",
        "gpus": [0], "ctx": 8192, "batch": 1024, "ubatch": 1024},
    "p100-qwen35-pxq3": {"model": f"{PXQ}/Qwen3.6-35B-A3B-PXQ3.gguf",
        "gpus": [0], "ctx": 8192, "batch": 1024, "ubatch": 1024},
    "dualv100-qwen35-pxq4": {"model": f"{PXQ}/Qwen3.6-35B-A3B-PXQ4.gguf",
        "gpus": [1, 2], "ctx": 8192, "batch": 2048, "ubatch": 2048, "split": "layer"},
}

PROMPT_SIZES = [128, 512, 2048, 4096]
GEN_TOKENS = 128

FILLER = ("The quick brown fox jumps over the lazy dog near the riverbank while "
          "the sun sets slowly behind the distant mountains and birds fly home. ")


def make_prompt(approx_tokens):
    reps = max(1, approx_tokens // 27)
    return ("Summarize the following text in one sentence.\n\n" + FILLER * reps +
            "\n\nSummary:")


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


def vram_used(gpus):
    total = 0
    for g in gpus:
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                                "--format=csv,noheader,nounits", "-i", str(g)],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
            total += int(r.stdout.strip().splitlines()[0])
        except Exception:
            return -1
    return total


def build_cmd(engine, tgt, spec):
    cfg = TARGETS[tgt]
    cmd = [ENGINES[engine]["bin"], "--model", cfg["model"],
           "--host", "127.0.0.1", "--port", str(PORT),
           "--gpu-layers", "999", "--flash-attn", "on",
           "--ctx-size", str(cfg["ctx"]), "--parallel", "1",
           "--batch-size", str(cfg["batch"]), "--ubatch-size", str(cfg["ubatch"])]
    if len(cfg["gpus"]) > 1:
        cmd += ["--split-mode", cfg.get("split", "layer")]
    if spec and engine == "fork":
        cmd += ["--spec-type", spec]
    return cmd


def launch(engine, tgt, spec):
    cfg = TARGETS[tgt]
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
           "CUDA_VISIBLE_DEVICES": ",".join(str(g) for g in cfg["gpus"])}
    env.update(ENGINES[engine]["env"])
    if len(cfg["gpus"]) > 1:
        # No-NVLink V100s: the CUDA VMM memory-pool peer path (cuMemSetAccess)
        # throws "unknown error" when split across GPUs — disable the VMM pool.
        env["GGML_CUDA_NO_VMM"] = "1"
    cmd = build_cmd(engine, tgt, spec)
    log = open(f"/tmp/pxq-bench-{engine}.log", "w")
    log.write("CMD: " + " ".join(cmd) + "\n")
    log.write("ENV: " + json.dumps({k: env.get(k) for k in
              ("CUDA_VISIBLE_DEVICES", "PXA_ENHANCE", "PXA_MODE", "LD_LIBRARY_PATH")}) + "\n")
    log.flush()
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                            preexec_fn=os.setsid)


def kill(p):
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
        for _ in range(60):
            if p.poll() is not None:
                break
            time.sleep(0.5)
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass


def wait_ready(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def probe(prompt, gen_tokens):
    payload = {"prompt": prompt, "n_predict": gen_tokens, "stream": True,
               "cache_prompt": False, "temperature": 0.0}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion", data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    ttft, timings = None, {}
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except Exception as e:
        print(f"    probe error: {e}")
        return None, {}
    for raw in resp:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            break
        try:
            obj = json.loads(body)
        except Exception:
            continue
        if ttft is None and obj.get("content"):
            ttft = time.time() - t0
        if obj.get("stop") and "timings" in obj:
            timings = obj["timings"]
    return ttft, timings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True, choices=list(ENGINES))
    ap.add_argument("--target", required=True, choices=list(TARGETS))
    ap.add_argument("--spec", default="", help="fork --spec-type value, e.g. mtp:n_max=1")
    ap.add_argument("--ub", type=int, default=0, help="override ubatch (and batch if --batch unset)")
    ap.add_argument("--batch", type=int, default=0, help="override batch")
    ap.add_argument("--ctx", type=int, default=0, help="override ctx-size")
    ap.add_argument("--no-restore", action="store_true")
    a = ap.parse_args()

    cfg = dict(TARGETS[a.target])
    if a.ctx:
        cfg["ctx"] = a.ctx
    if a.ub:
        cfg["ubatch"] = a.ub
        cfg["batch"] = a.batch or a.ub
    if a.batch:
        cfg["batch"] = a.batch
    TARGETS[a.target] = cfg
    print(f"=== {a.engine} on {a.target} (gpus {cfg['gpus']}, "
          f"ctx {cfg['ctx']}, ub {cfg['ubatch']}"
          f"{', spec ' + a.spec if a.spec else ''}) ===", flush=True)

    swap_unload_all()
    time.sleep(4)
    _keepalive["run"] = True
    threading.Thread(target=keepalive_unloader, daemon=True).start()

    rows, proc = [], None
    try:
        proc = launch(a.engine, a.target, a.spec)
        if not wait_ready():
            print("[FAIL] server did not become ready; tail:")
            print(subprocess.run(["tail", "-20", f"/tmp/pxq-bench-{a.engine}.log"],
                                 capture_output=True, text=True).stdout)
            return 1
        load_vram = vram_used(cfg["gpus"])
        print(f"ready; weights+ctx VRAM ~{load_vram} MiB", flush=True)
        probe(make_prompt(128), 16)  # warm
        for sz in PROMPT_SIZES:
            if sz + GEN_TOKENS + 256 > cfg["ctx"]:
                continue
            ttft, tim = probe(make_prompt(sz), GEN_TOKENS)
            if ttft is None:
                print(f"  prompt~{sz}: probe failed"); continue
            pn = tim.get("prompt_n", 0)
            pps = tim.get("prompt_per_second", 0) or 0
            tps = tim.get("predicted_per_second", 0) or 0
            peak = vram_used(cfg["gpus"])
            rows.append({"engine": a.engine, "target": a.target, "spec": a.spec,
                         "ubatch": cfg["ubatch"], "prompt_tokens": pn,
                         "ttft_s": round(ttft, 3),
                         "prefill_tok_s": round(pps, 1), "decode_tok_s": round(tps, 1),
                         "vram_mib": peak})
            print(f"  prompt~{sz:5} ({pn:5} tok): TTFT {ttft:6.3f}s | "
                  f"prefill {pps:8.1f} t/s | decode {tps:6.1f} t/s | VRAM {peak} MiB",
                  flush=True)
    finally:
        _keepalive["run"] = False
        time.sleep(1)
        if proc:
            kill(proc)
        if not a.no_restore:
            print("[restore] rewarming daily models...")
            subprocess.run(["python3", "/srv/ai/scripts/llama-swap-mode.py", "set", "daily"],
                           capture_output=True)

    if rows:
        os.makedirs(DATA_DIR, exist_ok=True)
        out = f"{DATA_DIR}/{a.target}.csv"
        exists = os.path.exists(out)
        with open(out, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["engine", "target", "spec", "ubatch",
                                              "prompt_tokens", "ttft_s", "prefill_tok_s",
                                              "decode_tok_s", "vram_mib"])
            if not exists:
                w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"CSV appended: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

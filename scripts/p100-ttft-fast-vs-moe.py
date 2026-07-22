#!/usr/bin/env python3
"""
P100 head-to-head: `fast` (Gemma-4-12B dense) vs `gemma-26b` (Gemma-4-26B-A4B MoE).

Focus: time-to-first-token (TTFT) and prompt-processing (prefill) speed, plus
decode throughput and VRAM, so we can decide whether the P100 slot should serve
the dense 12B or the MoE.

Method: unload all llama-swap models (and keep them unloaded) so the P100 is
clean, then for each model spawn a dedicated llama-server pinned to idx0 with the
same flags the roster uses (flash-attn on, ubatch 2048, ctx 8192 for the probe).
For a set of prompt sizes we submit a streaming /completion, measure TTFT
client-side (wall time to the first streamed token) and read the server's own
`timings` block for exact prefill (prompt_per_second) and decode
(predicted_per_second) rates. Restores the daily models on exit.

Run with any python3 (uses only stdlib):
  python3 scripts/p100-ttft-fast-vs-moe.py
"""
import argparse, json, os, re, signal, subprocess, sys, threading, time, urllib.request, urllib.error
from datetime import datetime

LLAMA_SERVER = "/srv/ai/src/llama.cpp/build/bin/llama-server"
MODELS_DIR = "/srv/ai/models"
SWAP = "http://127.0.0.1:9090"
PORT = 10098
GPU = 0  # P100 (PCI_BUS_ID ordering)
RESULTS_DIR = "/srv/ai/benchmarks/llm-scaling-bench/results"

MODELS = [
    ("fast-12b-dense", f"{MODELS_DIR}/gemma-4-12b/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf", ["--reasoning-budget", "0"]),
    ("gemma-26b-moe", f"{MODELS_DIR}/gemma-4-26b-a4b/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf", []),
]

# approximate prompt sizes in tokens (built by repeating a filler sentence)
PROMPT_SIZES = [128, 512, 2048, 6144]
GEN_TOKENS = 128
CTX = 16384


def http_post_stream(url, payload, timeout=600):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def swap_unload_all():
    for path in ("/unload", "/api/unload"):
        try:
            subprocess.run(["curl", "-s", "-m", "10", SWAP + path], capture_output=True, timeout=15)
        except Exception:
            pass


_keepalive = {"run": False}


def keepalive_unloader():
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


def launch(model_path, extra):
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(GPU)}
    cmd = [LLAMA_SERVER, "--model", model_path,
           "--host", "127.0.0.1", "--port", str(PORT),
           "--n-gpu-layers", "999", "--flash-attn", "on", "--cont-batching",
           "--ctx-size", str(CTX), "--parallel", "1",
           "--batch-size", "2048", "--ubatch-size", "2048"] + extra
    log = open("/tmp/p100-ttft.log", "w")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                            preexec_fn=os.setsid)


def kill(p):
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
        for _ in range(40):
            if p.poll() is not None:
                break
            time.sleep(0.5)
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass


def wait_ready(timeout=240):
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


FILLER = ("The quick brown fox jumps over the lazy dog near the riverbank while "
          "the sun sets slowly behind the distant mountains and birds fly home. ")


def make_prompt(approx_tokens):
    # ~13 words/sentence ~ 16 tokens; repeat to reach approx size
    reps = max(1, approx_tokens // 27)  # filler yields ~27 tokens/rep
    return ("Summarize the following text in one sentence.\n\n" + FILLER * reps +
            "\n\nSummary:")


def probe(prompt, gen_tokens):
    """Streaming /completion; return (ttft_s, timings dict)."""
    payload = {"prompt": prompt, "n_predict": gen_tokens, "stream": True,
               "cache_prompt": False, "temperature": 0.0, "timings_per_token": False}
    t0 = time.time()
    ttft = None
    timings = {}
    try:
        resp = http_post_stream(f"http://127.0.0.1:{PORT}/completion", payload)
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
    ap.add_argument("--no-restore", action="store_true")
    a = ap.parse_args()

    print("Unloading all llama-swap models to free the P100...")
    swap_unload_all()
    time.sleep(4)
    _keepalive["run"] = True
    threading.Thread(target=keepalive_unloader, daemon=True).start()

    rows = []
    proc = None
    try:
        for label, path, extra in MODELS:
            print(f"\n{'='*70}\n### {label}\nLaunching llama-server on P100 (idx0)...", flush=True)
            proc = launch(path, extra)
            if not wait_ready():
                print(f"[{label}] failed to start; tail:")
                print(subprocess.run(["tail", "-8", "/tmp/p100-ttft.log"],
                                     capture_output=True, text=True).stdout)
                kill(proc); proc = None
                continue
            load_vram = vram_used(GPU)
            print(f"[{label}] ready, weights+ctx VRAM ~{load_vram} MiB", flush=True)
            # warm once (kernels/graph)
            probe(make_prompt(128), 16)
            for sz in PROMPT_SIZES:
                p = make_prompt(sz)
                ttft, tim = probe(p, GEN_TOKENS)
                pn = tim.get("prompt_n", 0)
                pps = tim.get("prompt_per_second", 0) or 0
                tps = tim.get("predicted_per_second", 0) or 0
                pms = tim.get("prompt_ms", 0) or 0
                peak = vram_used(GPU)
                rows.append({"model": label, "prompt_tokens": pn,
                             "ttft_s": round(ttft, 3) if ttft else -1,
                             "prefill_tok_s": round(pps, 1),
                             "prefill_ms": round(pms, 1),
                             "decode_tok_s": round(tps, 1),
                             "vram_mib": peak})
                print(f"  prompt~{sz:5} ({pn:5} tok): TTFT {ttft:6.3f}s | "
                      f"prefill {pps:7.1f} tok/s | decode {tps:6.1f} tok/s | "
                      f"VRAM {peak} MiB", flush=True)
            kill(proc); proc = None
            time.sleep(2)
    finally:
        _keepalive["run"] = False
        time.sleep(1)
        if proc:
            kill(proc)
        if not a.no_restore:
            print("\n[restore] rewarming daily llama-swap models...")
            subprocess.run(["python3", "/srv/ai/scripts/llama-swap-mode.py", "set", "daily"],
                           capture_output=True)

    if rows:
        print(f"\n{'='*70}\n### Summary — P100 fast(12B dense) vs gemma-26b(MoE)")
        print(f"{'model':16} {'ptok':>6} {'TTFT s':>8} {'prefill t/s':>12} {'decode t/s':>11}")
        for r in rows:
            print(f"{r['model']:16} {r['prompt_tokens']:6} {r['ttft_s']:8} "
                  f"{r['prefill_tok_s']:12} {r['decode_tok_s']:11}")
        os.makedirs(RESULTS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = f"{RESULTS_DIR}/p100_ttft_fast_vs_moe_{ts}.csv"
        import csv
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["model", "prompt_tokens", "ttft_s",
                                              "prefill_tok_s", "prefill_ms",
                                              "decode_tok_s", "vram_mib"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\nCSV: {out}")


if __name__ == "__main__":
    main()

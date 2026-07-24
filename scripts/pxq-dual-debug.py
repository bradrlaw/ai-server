#!/usr/bin/env python3
"""Isolate the pxq_llama dual-V100 decode collapse (~17 t/s vs stock ~92 t/s).

For each named config we launch the fork (or stock) llama-server on the two
V100s (idx1+idx2, PCI_BUS_ID order), run a short + a 4k-prompt decode probe at
batch=1, and record decode/prefill t/s. We also scrape the startup log for the
diagnostics the fork author asked about: number of graph splits, per-device
tensor/buffer assignment, ROUTER_FUSE / PXA wiring, and peer-access decisions.

Goal: pin down which lever (PXA_ENHANCE, ROUTER_FUSE, GGML_CUDA_NO_VMM, split
mode, quant class) turns the flat ~17 t/s cross-GPU sync penalty on/off on our
no-NVLink PHB-topology box, so the author can reproduce or fix it.

Usage:
  python3 scripts/pxq-dual-debug.py                # run the full matrix
  python3 scripts/pxq-dual-debug.py --only repro q6k-fork enhance-off
  python3 scripts/pxq-dual-debug.py --ctx 8192 --gen 64
Writes per-config logs to /tmp/pxq-dual-debug/<name>.log and a summary CSV to
docs/data/pxq/dual-debug.csv.
"""
import argparse, csv, json, os, signal, subprocess, time, urllib.request

MODELS = "/srv/ai/models"
STOCK_BIN = "/srv/ai/src/llama.cpp/build/bin/llama-server"
FORK2 = "/srv/ai/src/pxq_llama/pxq_llama-2026-07-23-linux-x64"
NCCL = "/srv/ai/venvs/comfyui/lib/python3.12/site-packages/nvidia/nccl/lib"
FORK2_LD = ":".join([f"{FORK2}/bin", f"{FORK2}/src", f"{FORK2}/ggml/src",
                     f"{FORK2}/examples/mtmd", "/usr/local/cuda/lib64", NCCL])
FORK2_BIN = f"{FORK2}/bin/llama-server"
PXQ4 = f"{MODELS}/pxq/Qwen3.6-35B-A3B-PXQ4.gguf"
Q6K = f"{MODELS}/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q6_K.gguf"
PORT = 8971
GPUS = "1,2"           # both V100s, PCI_BUS_ID order
LOGDIR = "/tmp/pxq-dual-debug"

# Each config: model, engine bin, extra server flags, and env overrides.
# base fork2 env = PXA_ENHANCE=1 + GGML_CUDA_NO_VMM=1 (our working repro).
def cfgs():
    layer = ["--split-mode", "layer", "--tensor-split", "1,1"]
    row = ["--split-mode", "row", "--tensor-split", "1,1"]
    fork_kv = ["--cache-type-k", "f16", "--cache-type-v", "f16", "--jinja"]
    base_env = {"LD_LIBRARY_PATH": FORK2_LD, "PXA_ENHANCE": "1", "GGML_CUDA_NO_VMM": "1"}
    def fork(model, flags, env_extra=None, drop=None):
        e = dict(base_env)
        if env_extra:
            e.update(env_extra)
        for k in (drop or []):
            e.pop(k, None)
        return {"bin": FORK2_BIN, "model": model, "flags": flags + fork_kv, "env": e}
    return {
        # --- the reproduction: fork PXQ4, layer split, our working env ---
        "repro-pxq4-layer": fork(PXQ4, layer),
        # --- KEY: does the fork also collapse on a STANDARD quant? (never tested) ---
        "q6k-fork-layer": fork(Q6K, layer),
        # --- ENHANCE / ROUTER_FUSE levers (author's suspects) ---
        "pxq4-enhance-off": fork(PXQ4, layer, drop=["PXA_ENHANCE"]),
        "pxq4-routerfuse-off": fork(PXQ4, layer, {"PXA_ROUTER_FUSE": "0"}),
        # --- the env difference from the author's box: we force NO_VMM ---
        "pxq4-vmm-on": fork(PXQ4, layer, drop=["GGML_CUDA_NO_VMM"]),
        # --- split mode ---
        "pxq4-row": fork(PXQ4, row),
        # --- peer-access off: force host-staged copies (isolate P2P path) ---
        "pxq4-no-peer": fork(PXQ4, layer, {"GGML_CUDA_PEER_MAX_BATCH_SIZE": "0"}),
        # --- single-card reference (should be ~97 t/s) ---
        "pxq4-single": {"bin": FORK2_BIN, "model": PXQ4,
                        "flags": ["--cache-type-k", "f16", "--cache-type-v", "f16", "--jinja"],
                        "env": dict(base_env), "gpus": "1"},
        # --- stock llama.cpp on Q6_K dual-split (the ~92 t/s baseline) ---
        "q6k-stock-layer": {"bin": STOCK_BIN, "model": Q6K, "flags": layer, "env": {}},
        # === Round 4b: toggle the fork's own multi-GPU execution path ===
        # -smgs forces "Split Mode Graph Scheduling" (default off) on the layer split.
        "pxq4-smgs1": fork(PXQ4, layer + ["--split-mode-graph-scheduling"]),
        # the fork's dedicated -sm graph mode (splits tensors + compute graph).
        "pxq4-sm-graph": fork(PXQ4, ["--split-mode", "graph", "--tensor-split", "1,1"]),
        # async compute-graph evaluation.
        "pxq4-async": fork(PXQ4, layer + ["--scheduler_async"]),
        # smgs + async together.
        "pxq4-smgs1-async": fork(PXQ4, layer + ["--split-mode-graph-scheduling",
                                                "--scheduler_async"]),
        # the fork's -sm graph on a standard Q6_K (is the fix quant-independent?).
        "q6k-sm-graph": fork(Q6K, ["--split-mode", "graph", "--tensor-split", "1,1"]),
    }


def launch(cfg, ctx, sched_debug):
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
           "CUDA_VISIBLE_DEVICES": cfg.get("gpus", GPUS)}
    env.update(cfg["env"])
    if sched_debug:
        env["GGML_SCHED_DEBUG"] = "2"
        env["LLAMA_LOG_VERBOSITY"] = "1"
    cmd = [cfg["bin"], "--model", cfg["model"], "--host", "127.0.0.1", "--port", str(PORT),
           "--gpu-layers", "999", "--flash-attn", "on", "--ctx-size", str(ctx),
           "--parallel", "1", "--batch-size", "2048", "--ubatch-size", "2048"] + cfg["flags"]
    log = open(f"{LOGDIR}/{cfg['_name']}.log", "w")
    log.write("CMD: " + " ".join(cmd) + "\n")
    log.write("ENV: " + json.dumps({k: env.get(k) for k in
              ("CUDA_VISIBLE_DEVICES", "PXA_ENHANCE", "PXA_ROUTER_FUSE",
               "GGML_CUDA_NO_VMM", "GGML_CUDA_PEER_MAX_BATCH_SIZE")}) + "\n\n")
    log.flush()
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                         preexec_fn=os.setsid)
    return p, log


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
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def probe(prompt, gen):
    payload = {"prompt": prompt, "n_predict": gen, "stream": True,
               "cache_prompt": False, "temperature": 0.0}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    timings = {}
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except Exception as e:
        print(f"    probe error: {e}")
        return {}
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
        if obj.get("stop") and "timings" in obj:
            timings = obj["timings"]
    return timings


DIAG_KEYS = ("graph splits", "ROUTER_FUSE", "PXA level", "CUBLAS", "FA_PREFILL",
             "split_mode", "assigned", "peer", "VMM", "buffer size", "flash")


def scrape(name):
    hits = []
    try:
        with open(f"{LOGDIR}/{name}.log") as f:
            for ln in f:
                low = ln.lower()
                if any(k.lower() in low for k in DIAG_KEYS):
                    hits.append(ln.rstrip())
    except Exception:
        pass
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--gen", type=int, default=64)
    ap.add_argument("--only", nargs="+", default=None, help="subset of config names")
    ap.add_argument("--sched-debug", action="store_true",
                    help="set GGML_SCHED_DEBUG=2 to dump graph-split placement")
    a = ap.parse_args()
    os.makedirs(LOGDIR, exist_ok=True)
    allc = cfgs()
    names = a.only or list(allc)
    short = "The quick brown fox. " * 8
    long4k = "The quick brown fox jumps over the lazy dog. " * 520  # ~4k tokens
    results = []
    for name in names:
        cfg = dict(allc[name]); cfg["_name"] = name
        if not os.path.exists(cfg["model"]):
            print(f"[{name}] SKIP — model missing: {cfg['model']}")
            continue
        print(f"\n===== {name} =====")
        p, log = launch(cfg, a.ctx, a.sched_debug)
        try:
            if not wait_ready():
                print(f"[{name}] server did not become ready; see {LOGDIR}/{name}.log")
                continue
            probe(short, a.gen)                       # warm
            t_short = probe(short, a.gen)
            t_long = probe(long4k, a.gen)
            dec_s = t_short.get("predicted_per_second")
            dec_l = t_long.get("predicted_per_second")
            pre_l = t_long.get("prompt_per_second")
            print(f"[{name}] decode short={dec_s and round(dec_s,1)} t/s  "
                  f"4k={dec_l and round(dec_l,1)} t/s  prefill4k={pre_l and round(pre_l,1)} t/s")
            results.append({"config": name, "decode_short": dec_s,
                            "decode_4k": dec_l, "prefill_4k": pre_l})
        finally:
            kill(p); log.close()
        for h in scrape(name)[:12]:
            print("   |", h)
        time.sleep(4)
    if results:
        out = "/srv/ai/docs/data/pxq/dual-debug.csv"
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["config", "decode_short", "decode_4k", "prefill_4k"])
            w.writeheader(); w.writerows(results)
        print(f"\nCSV: {out}")
        print(f"{'config':22} {'dec_short':>10} {'dec_4k':>8} {'prefill_4k':>11}")
        for r in results:
            print(f"{r['config']:22} {str(round(r['decode_short'],1) if r['decode_short'] else '-'):>10} "
                  f"{str(round(r['decode_4k'],1) if r['decode_4k'] else '-'):>8} "
                  f"{str(round(r['prefill_4k'],1) if r['prefill_4k'] else '-'):>11}")


if __name__ == "__main__":
    main()

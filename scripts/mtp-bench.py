#!/usr/bin/env python3
"""Apples-to-apples MTP benchmark for our daily `chat` model on stock llama.cpp.

Compares Qwen3.6-35B-A3B UD-Q6_K with and without the model's built-in
Multi-Token-Prediction (MTP) speculative-decode head, on a single V100 (idx1),
using the same serving flags as the `chat` slot (q8_0 KV, --parallel 1,
batch/ubatch 2048, flash-attn on).

The regular unsloth GGUF we serve was converted WITHOUT the MTP head; the
`-MTP-GGUF` repo embeds it (extra blk.40.nextn.* tensors). Stock llama.cpp
enables it with `--spec-type draft-mtp --spec-draft-n-max N` (head is in-model,
no separate draft file).

Runs a baseline (no MTP) then an n_max sweep, at several prompt sizes, and
records prefill / decode / draft-acceptance / VRAM to docs/data/mtp/.

  benchmarks/llm-scaling-bench/.venv/bin/python scripts/mtp-bench.py     # not needed; stdlib only
  python3 scripts/mtp-bench.py --nmax 0 1 2 3 4

Always exports CUDA_DEVICE_ORDER=PCI_BUS_ID and keeps the target GPU clear by
repeatedly unloading llama-swap models while it runs; re-warms daily at the end
unless --no-restore.
"""
import argparse, csv, json, os, signal, socket, subprocess, sys, threading, time
import urllib.request

MODEL = "/srv/ai/models/qwen3.6-35b-a3b-mtp/Qwen3.6-35B-A3B-UD-Q6_K.gguf"
STOCK_BIN = "/srv/ai/src/llama.cpp/build/bin/llama-server"
SWAP = "http://127.0.0.1:9090"
DATA_DIR = "/srv/ai/docs/data/mtp"
GPU = 1                     # free V100 for testing (idx1)
CTX = 32768                 # test ctx (weights ~28GB + MTP; leaves headroom on 32GB)
BATCH = 2048
PROMPT_SIZES = [128, 512, 2048, 4096]
GEN_TOKENS = 256            # longer gen -> steadier decode + MTP acceptance signal


def pick_port():
    for p in range(10090, 10130):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p)); s.close(); return p
        except OSError:
            s.close()
    return 10099


PORT = pick_port()

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


def vram_used(gpu):
    """Sum memory.used across one or more comma-separated GPU indices."""
    try:
        total = 0
        for g in str(gpu).split(","):
            r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                                "--format=csv,noheader,nounits", "-i", g.strip()],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
            total += int(r.stdout.strip().splitlines()[0])
        return total
    except Exception:
        return -1


KV_TYPE = "q8_0"       # KV cache quant (big uses f16); set via --kv
SPLIT = False          # --split-mode layer across multiple GPUs (dual-V100 big)


def build_cmd(nmax, ctx=CTX):
    cmd = [STOCK_BIN, "--model", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "--gpu-layers", "999", "--flash-attn", "on", "--ctx-size", str(ctx),
           "--parallel", "1", "--batch-size", str(BATCH), "--ubatch-size", str(BATCH),
           "--cache-type-k", KV_TYPE, "--cache-type-v", KV_TYPE]
    if SPLIT:
        cmd += ["--split-mode", "layer"]
    if nmax > 0:
        cmd += ["--spec-type", "draft-mtp", "--spec-draft-n-max", str(nmax)]
    return cmd


def launch(nmax, ctx=CTX):
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
           "CUDA_VISIBLE_DEVICES": str(GPU)}
    cmd = build_cmd(nmax, ctx)
    log = open(f"/tmp/mtp-bench-{nmax}.log", "w")
    log.write("CMD: " + " ".join(cmd) + "\n"); log.flush()
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
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    ttft, timings = None, {}
    try:
        resp = urllib.request.urlopen(req, timeout=900)
    except Exception as e:
        print(f"    probe error: {e}"); return None, {}
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


def accept_rate(tim):
    """Draft acceptance from server timings, if the build reports it."""
    dn = tim.get("draft_n") or tim.get("n_draft") or 0
    da = tim.get("draft_n_accepted") or tim.get("n_draft_accepted") or 0
    if dn:
        return round(100.0 * da / dn, 1)
    return None


def main():
    global MODEL, GPU, KV_TYPE, SPLIT
    ap = argparse.ArgumentParser()
    ap.add_argument("--nmax", type=int, nargs="+", default=[0, 1, 2, 3, 4],
                    help="MTP n_max values to test; 0 = baseline (MTP off)")
    ap.add_argument("--ctx", type=int, default=CTX, help="context size to launch with")
    ap.add_argument("--fill", type=int, default=0,
                    help="if set, probe a single prompt of ~this many tokens (VRAM ceiling test)")
    ap.add_argument("--model", default=MODEL, help="MTP-equipped GGUF to benchmark")
    ap.add_argument("--gpu", type=str, default=str(GPU),
                    help="GPU index to pin, or comma list e.g. '1,2' for dual-card split")
    ap.add_argument("--kv", default="q8_0", help="KV cache type (big uses f16)")
    ap.add_argument("--split", action="store_true",
                    help="--split-mode layer across the GPUs in --gpu (dual-V100 big)")
    ap.add_argument("--label", default="qwen35-chat",
                    help="model label: names the CSV (<label>-mtp.csv) and the 'model' column")
    ap.add_argument("--no-restore", action="store_true")
    a = ap.parse_args()
    MODEL, GPU = a.model, a.gpu
    KV_TYPE, SPLIT = a.kv, a.split
    ctx = a.ctx
    sizes = [a.fill] if a.fill else PROMPT_SIZES

    os.makedirs(DATA_DIR, exist_ok=True)
    out = (f"{DATA_DIR}/{a.label}-mtp-ctxfit.csv" if a.fill
           else f"{DATA_DIR}/{a.label}-mtp.csv")
    exists = os.path.exists(out)
    fout = open(out, "a", newline="")
    w = csv.DictWriter(fout, fieldnames=["engine", "model", "nmax", "prompt_tokens",
                                         "ttft_s", "prefill_tok_s", "decode_tok_s",
                                         "accept_pct", "vram_mib"])
    if not exists:
        w.writeheader()

    swap_unload_all(); time.sleep(4)
    _keepalive["run"] = True
    threading.Thread(target=keepalive_unloader, daemon=True).start()

    try:
        for nmax in a.nmax:
            label = "baseline (MTP off)" if nmax == 0 else f"MTP n_max={nmax}"
            print(f"\n=== {label} — {a.label} {MODEL.split('/')[-1]}, GPU {GPU}"
                  f"{' split-layer' if SPLIT else ''}, ctx {ctx}, {KV_TYPE} KV ===", flush=True)
            proc = None
            try:
                proc = launch(nmax, ctx)
                if not wait_ready():
                    print("[FAIL] server not ready; tail:")
                    print(subprocess.run(["tail", "-25", f"/tmp/mtp-bench-{nmax}.log"],
                                         capture_output=True, text=True).stdout)
                    continue
                print(f"ready; weights+ctx VRAM ~{vram_used(GPU)} MiB", flush=True)
                probe(make_prompt(128), 16)  # warm
                for sz in sizes:
                    if sz + GEN_TOKENS + 256 > ctx:
                        print(f"  prompt~{sz}: skipped (exceeds ctx {ctx})"); continue
                    ttft, tim = probe(make_prompt(sz), GEN_TOKENS)
                    if ttft is None:
                        print(f"  prompt~{sz}: probe failed"); continue
                    pn = tim.get("prompt_n", 0)
                    pps = tim.get("prompt_per_second", 0) or 0
                    tps = tim.get("predicted_per_second", 0) or 0
                    acc = accept_rate(tim)
                    peak = vram_used(GPU)
                    w.writerow({"engine": "stock", "model": a.label, "nmax": nmax,
                                "prompt_tokens": pn, "ttft_s": round(ttft, 3),
                                "prefill_tok_s": round(pps, 1),
                                "decode_tok_s": round(tps, 1),
                                "accept_pct": acc if acc is not None else "",
                                "vram_mib": peak})
                    fout.flush()
                    accs = f" | accept {acc:4.1f}%" if acc is not None else ""
                    print(f"  prompt~{sz:5} ({pn:5} tok): TTFT {ttft:6.3f}s | "
                          f"prefill {pps:8.1f} t/s | decode {tps:6.1f} t/s{accs} | "
                          f"VRAM {peak} MiB", flush=True)
            finally:
                if proc:
                    kill(proc)
                time.sleep(2)
    finally:
        _keepalive["run"] = False
        fout.close()
        if not a.no_restore:
            print("\n[restore] rewarming daily models...")
            subprocess.run(["python3", "/srv/ai/scripts/llama-swap-mode.py", "set", "daily"],
                           capture_output=True)
    print(f"\nCSV: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

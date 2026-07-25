#!/usr/bin/env python3
"""MTP speculative-decode benefit by *prompt type* and *temperature*.

Motivation: a Mac-Studio (M2 Ultra, Metal) user reported that Qwen3.6-27B MTP
helps for coding (temp=0) but *hurts* for creative writing (temp=1) even with
tuned params. Is that a Metal-platform artefact, or an intrinsic property of the
prompt type / sampling temperature? This replicates the test on our CUDA/V100
platform with the same model class (Qwen3.6-27B, in-model MTP head).

To disentangle *content* from *temperature* (their test bundles code=temp0,
prose/creative=temp1), we run the full matrix: {code, technical-prose, creative}
x {temp 0.0, temp 1.0} x {baseline, MTP n_max=2, MTP n_max=5}, and record decode
tok/s + draft acceptance. MTP draft acceptance falls when the sampled token
diverges from the greedy draft, which is exactly what high temperature and
low-predictability content cause -- so if we reproduce the pattern here, it's the
prompt/sampling, not the Mac.

Qwen3.6-27B is a reasoning model; we DISABLE thinking (prefill an empty
<think></think> block) so decode is measured over the actual code/prose/creative
output, not uniform reasoning text.

  python3 scripts/mtp-scenario-bench.py                 # full matrix
  python3 scripts/mtp-scenario-bench.py --nmax 0 2 5 --temps 0 1

Pins CUDA_DEVICE_ORDER=PCI_BUS_ID, keeps GPU idx1 clear by unloading llama-swap
models while it runs, re-warms daily at the end unless --no-restore.
"""
import argparse, csv, json, os, signal, socket, subprocess, sys, threading, time
import urllib.request

MODEL = "/srv/ai/models/qwen3.6-27b-mtp/Qwen3.6-27B-Q6_K.gguf"
STOCK_BIN = "/srv/ai/src/llama.cpp/build/bin/llama-server"
SWAP = "http://127.0.0.1:9090"
DATA_DIR = "/srv/ai/docs/data/mtp"
GPU = 1
CTX = 8192
BATCH = 2048
GEN_TOKENS = 320            # long enough for a steady per-type decode + accept signal

# (name, user_prompt) -- content types that plausibly differ in token predictability.
SCENARIOS = [
    ("code", "Write a complete Python implementation of an LRU cache class named "
             "LRUCache backed by an OrderedDict, with get(key) and put(key, value) "
             "methods and a capacity limit. Include a short docstring on each method "
             "and a couple of usage examples at the bottom."),
    ("technical-prose", "Explain how TCP congestion control works. Cover slow start, "
             "congestion avoidance, fast retransmit and fast recovery, the congestion "
             "window, and how the algorithm reacts to packet loss versus ACKs. Write "
             "it as clear technical prose for an engineer."),
    ("creative", "Write an imaginative short story about a lighthouse keeper on a "
             "remote island who finds a glass bottle containing a handwritten letter "
             "from someone who claims to be living one hundred years in the future. "
             "Make it vivid and original."),
]

# Qwen3.6 chat template with thinking disabled via a prefilled empty think block.
def make_prompt(user):
    return ("<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"
            "<|im_start|>user\n" + user + "<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def pick_port():
    for p in range(10090, 10130):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p)); s.close(); return p
        except OSError:
            s.close()
    return 10099


PORT = pick_port()


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
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                            "--format=csv,noheader,nounits", "-i", str(gpu)],
                           capture_output=True, text=True, timeout=10,
                           env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return -1


def build_cmd(nmax):
    cmd = [STOCK_BIN, "--model", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "--gpu-layers", "999", "--flash-attn", "on", "--ctx-size", str(CTX),
           "--parallel", "1", "--batch-size", str(BATCH), "--ubatch-size", str(BATCH),
           "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--jinja"]
    if nmax > 0:
        cmd += ["--spec-type", "draft-mtp", "--spec-draft-n-max", str(nmax)]
    return cmd


def launch(nmax):
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
           "CUDA_VISIBLE_DEVICES": str(GPU)}
    cmd = build_cmd(nmax)
    log = open(f"/tmp/mtp-scen-{nmax}.log", "w")
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


def probe(prompt, gen_tokens, temp, seed=1234):
    payload = {"prompt": prompt, "n_predict": gen_tokens, "stream": True,
               "cache_prompt": False, "temperature": temp, "seed": seed,
               "top_p": 0.95, "top_k": 40}
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
    dn = tim.get("draft_n") or tim.get("n_draft") or 0
    da = tim.get("draft_n_accepted") or tim.get("n_draft_accepted") or 0
    if dn:
        return round(100.0 * da / dn, 1)
    return None


def main():
    global MODEL, GPU
    ap = argparse.ArgumentParser()
    ap.add_argument("--nmax", type=int, nargs="+", default=[0, 2, 5],
                    help="MTP n_max values; 0 = baseline (MTP off)")
    ap.add_argument("--temps", type=float, nargs="+", default=[0.0, 1.0])
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--gpu", type=int, default=GPU)
    ap.add_argument("--gen", type=int, default=GEN_TOKENS)
    ap.add_argument("--label", default="scenario-sweep")
    ap.add_argument("--no-restore", action="store_true")
    a = ap.parse_args()
    MODEL, GPU = a.model, a.gpu

    os.makedirs(DATA_DIR, exist_ok=True)
    out = f"{DATA_DIR}/{a.label}.csv"
    exists = os.path.exists(out)
    fout = open(out, "a", newline="")
    w = csv.DictWriter(fout, fieldnames=["model", "scenario", "temp", "nmax",
                                         "prompt_tokens", "gen_tokens", "ttft_s",
                                         "prefill_tok_s", "decode_tok_s",
                                         "accept_pct", "vram_mib"])
    if not exists:
        w.writeheader()

    swap_unload_all(); time.sleep(4)
    _keepalive["run"] = True
    threading.Thread(target=keepalive_unloader, daemon=True).start()

    try:
        for nmax in a.nmax:
            tag = "baseline (MTP off)" if nmax == 0 else f"MTP n_max={nmax}"
            print(f"\n=== {tag} — {MODEL.split('/')[-1]}, GPU {GPU}, ctx {CTX} ===",
                  flush=True)
            proc = None
            try:
                proc = launch(nmax)
                if not wait_ready():
                    print("[FAIL] server not ready; tail:")
                    print(subprocess.run(["tail", "-25", f"/tmp/mtp-scen-{nmax}.log"],
                                         capture_output=True, text=True).stdout)
                    continue
                print(f"ready; VRAM ~{vram_used(GPU)} MiB", flush=True)
                probe(make_prompt("Say hi."), 16, 0.0)  # warm
                for sname, sprompt in SCENARIOS:
                    for temp in a.temps:
                        ttft, tim = probe(make_prompt(sprompt), a.gen, temp)
                        if ttft is None:
                            print(f"  {sname:16} T={temp}: probe failed"); continue
                        pn = tim.get("prompt_n", 0)
                        gn = tim.get("predicted_n", 0)
                        pps = tim.get("prompt_per_second", 0) or 0
                        tps = tim.get("predicted_per_second", 0) or 0
                        acc = accept_rate(tim)
                        peak = vram_used(GPU)
                        w.writerow({"model": "qwen3.6-27b", "scenario": sname,
                                    "temp": temp, "nmax": nmax, "prompt_tokens": pn,
                                    "gen_tokens": gn, "ttft_s": round(ttft, 3),
                                    "prefill_tok_s": round(pps, 1),
                                    "decode_tok_s": round(tps, 1),
                                    "accept_pct": acc if acc is not None else "",
                                    "vram_mib": peak})
                        fout.flush()
                        accs = f" | accept {acc:5.1f}%" if acc is not None else " | accept    -- "
                        print(f"  {sname:16} T={temp:<3} ({gn:4} tok): "
                              f"decode {tps:6.1f} t/s{accs} | prefill {pps:7.1f} t/s",
                              flush=True)
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

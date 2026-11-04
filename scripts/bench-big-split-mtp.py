#!/usr/bin/env python3
"""`big` (Qwen3.8-27B UD-Q8_K_XL, dual V100) served by llama-server: -sm layer vs -sm tensor,
each with and without draft-mtp. Measures decode tok/s, MTP acceptance and prefill via the
server's own `timings`. Own server on a private port; refuses if the V100s are busy.

  V100_IDX=0,1 scripts/bench-big-split-mtp.py --label x16 [--ctx 32768] [--tokens 400]

Production `big` = layer + draft-mtp n=2 (ctx 262144, reasoning_effort medium). Test ctx is
smaller (KV size doesn't change tok/s much at these depths). Results: docs/data/big-split/.
Servers are killed afterwards; restore models with scripts/llama-swap-mode.py set daily.
"""
import argparse, csv, json, os, signal, statistics, subprocess, sys, time, urllib.request

ROOT = "/srv/ai"
BIN = os.environ.get("LLAMA_SERVER_BIN", f"{ROOT}/src/llama.cpp/build/bin/llama-server")
MODEL = f"{ROOT}/models/qwen3.8-27b/Qwen3.8-27B-UD-Q8_K_XL.gguf"
OUT = os.environ.get("BIG_SPLIT_OUT", f"{ROOT}/docs/data/big-split")
IDX = os.environ.get("V100_IDX", "1,2")
PORT = 18931
PROMPTS = {
    "code": "Write a complete Python module implementing a thread-safe LRU cache with TTL expiry, type hints, and unit tests.",
    "prose": "Explain in detail how a B-tree differs from an LSM tree for write-heavy workloads, with trade-offs.",
    "json": "Produce a JSON array of 40 objects with fields id, name, email, active, tags. Output only JSON.",
}
LONG_WORDS = 3500   # ~5k-token prompt for a prefill figure


def post(path, body, timeout=900):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def start(mode, mtp, ctx):
    cmd = [BIN, "--model", MODEL, "--ctx-size", str(ctx), "--cache-type-k", "f16", "--cache-type-v", "f16",
           "--split-mode", mode, "--parallel", "1", "--host", "127.0.0.1", "--port", str(PORT),
           "--n-gpu-layers", "999", "--flash-attn", "on", "--jinja", "--no-warmup",
           "--chat-template-kwargs", '{"enable_thinking":false}']
    if mtp:
        cmd += ["--spec-type", "draft-mtp", "--spec-draft-n-max", "2"]
    log = open(f"{OUT}/{ARGS.label}-{mode}-{'mtp' if mtp else 'nomtp'}.server.log", "w")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=IDX), start_new_session=True)
    for _ in range(300):
        if p.poll() is not None:
            return None
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).status == 200:
                return p
        except Exception:
            time.sleep(2)
    p.kill()
    return None


def stop(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(30)
    except Exception:
        os.killpg(p.pid, signal.SIGKILL)
    time.sleep(5)


def chat(prompt, n):
    return post("/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": prompt}], "max_tokens": n,
                                         "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}})


def used_mib():
    q = subprocess.check_output(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"], text=True)
    ids = set(IDX.split(","))
    return sum(int(l.split(",")[1]) for l in q.splitlines() if l.split(",")[0].strip() in ids)


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--tokens", type=int, default=400)
    ap.add_argument("--reps", type=int, default=2)
    ARGS = a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    if used_mib() > 2000:
        sys.exit(f"V100s ({IDX}) hold {used_mib()} MiB; unload models first (POST :9090/api/models/unload)")
    rows = []
    for mode, mtp in (("layer", True), ("tensor", True), ("layer", False), ("tensor", False)):
        tag = f"{mode}{'+mtp' if mtp else ''}"
        print(f"== {tag} ==", flush=True)
        t0 = time.time()
        p = start(mode, mtp, a.ctx)
        if p is None:
            print(f"  FAILED to start (see {OUT}/{a.label}-{mode}-{'mtp' if mtp else 'nomtp'}.server.log)")
            rows.append({"config": tag, "kind": "start_failed"})
            continue
        load_s = round(time.time() - t0, 1)
        try:
            chat("Say hi.", 8)
            vram = used_mib()
            for kind, pr in PROMPTS.items():
                for rep in range(a.reps):
                    r = chat(pr, a.tokens)
                    t = r["timings"]
                    acc = (t.get("draft_n_accepted", 0) / t["draft_n"]) if t.get("draft_n") else None
                    rows.append({"config": tag, "kind": kind, "decode_tps": round(t["predicted_per_second"], 2),
                                 "accept": round(acc, 3) if acc is not None else None, "n": t["predicted_n"],
                                 "text_ok": bool(r["choices"][0]["message"].get("content"))})
            long_text = " ".join(f"Record {i}: the scheduler reconciles pending invoices under contention." for i in range(LONG_WORDS // 9))
            r = chat(long_text + "\n\nIn one sentence, what is this about?", 32)
            t = r["timings"]
            rows.append({"config": tag, "kind": "prefill", "prompt_n": t["prompt_n"], "prefill_tps": round(t["prompt_per_second"], 1)})
            rows.append({"config": tag, "kind": "meta", "load_s": load_s, "vram_mib": vram})
            # determinism check: greedy output of the same prompt, first 200 chars
            rows.append({"config": tag, "kind": "sample", "text": chat(PROMPTS["json"], 60)["choices"][0]["message"]["content"][:200]})
        except Exception as e:
            print(f"  ERROR: {e}")
            rows.append({"config": tag, "kind": "error", "text": str(e)[:200]})
        finally:
            stop(p)
    with open(f"{OUT}/{a.label}-mtp-summary.json", "w") as f:
        json.dump(rows, f, indent=1)
    print(f"\n{'config':<14}{'code':>8}{'prose':>8}{'json':>8}{'accept':>8}{'prefill':>9}{'VRAM MiB':>10}")
    for tag in dict.fromkeys(r["config"] for r in rows):
        rr = [r for r in rows if r["config"] == tag]
        d = {k: statistics.median([r["decode_tps"] for r in rr if r["kind"] == k] or [0]) for k in PROMPTS}
        acc = [r["accept"] for r in rr if r.get("accept") is not None]
        pf = next((r["prefill_tps"] for r in rr if r["kind"] == "prefill"), 0)
        vm = next((r["vram_mib"] for r in rr if r["kind"] == "meta"), 0)
        print(f"{tag:<14}{d['code']:>8}{d['prose']:>8}{d['json']:>8}{(f'{statistics.mean(acc):.0%}' if acc else '-'):>8}{pf:>9}{vm:>10}")
    samples = {r["config"]: r["text"] for r in rows if r["kind"] == "sample"}
    print("\nGreedy JSON sample identical across configs:", len(set(samples.values())) == 1 if samples else "n/a")


if __name__ == "__main__":
    main()

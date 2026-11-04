#!/usr/bin/env python3
"""`big` long-context check: -sm layer vs tensor, both with draft-mtp n=2, ctx 262144 f16 KV.
Prompts grow 16k -> 64k -> 128k (shared prefix, so each step prefills only the new part); at each depth
it asks for a needle planted at the start plus a ~200-token summary, recording prefill tok/s,
decode tok/s, MTP acceptance and VRAM. Own server on port 18931; refuses if the V100s are busy.

  V100_IDX=0,1 scripts/bench-big-long-ctx.py --label x16 [--depths 16000,64000,128000]
"""
import argparse, json, os, signal, subprocess, sys, time, urllib.request

ROOT = "/srv/ai"
BIN = os.environ.get("LLAMA_SERVER_BIN", f"{ROOT}/src/llama.cpp/build/bin/llama-server")
MODEL = f"{ROOT}/models/qwen3.8-27b/Qwen3.8-27B-UD-Q8_K_XL.gguf"
OUT = os.environ.get("BIG_SPLIT_OUT", f"{ROOT}/docs/data/big-split")
IDX = os.environ.get("V100_IDX", "1,2")
PORT = 18931
NEEDLE = "AMBER-HERON-4471"


def post(path, body, timeout=3600):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def used_mib():
    q = subprocess.check_output(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"], text=True)
    ids = set(IDX.split(","))
    return sum(int(l.split(",")[1]) for l in q.splitlines() if l.split(",")[0].strip() in ids)


def start(mode, ctx, label):
    cmd = [BIN, "--model", MODEL, "--ctx-size", str(ctx), "--cache-type-k", "f16", "--cache-type-v", "f16",
           "--split-mode", mode, "--parallel", "1", "--host", "127.0.0.1", "--port", str(PORT),
           "--n-gpu-layers", "999", "--flash-attn", "on", "--jinja", "--no-warmup",
           "--spec-type", "draft-mtp", "--spec-draft-n-max", "2"]
    log = open(f"{OUT}/{label}-longctx-{mode}.server.log", "w")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                         env=dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=IDX))
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


def line(i):
    return f"Log {i}: node n{i * 7919 % 1009} reported value {i * 104729 % 99991} at epoch {1700000000 + i * 37}; status {'ok' if i % 5 else 'retry'}.\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--ctx", type=int, default=262144)
    ap.add_argument("--depths", default="16000,64000,128000")
    ap.add_argument("--modes", default="layer,tensor")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    if used_mib() > 2000:
        sys.exit(f"V100s ({IDX}) hold {used_mib()} MiB; unload models first")
    rows = []
    for mode in a.modes.split(","):
        print(f"== {mode}+mtp ctx {a.ctx} ==", flush=True)
        t0 = time.time()
        p = start(mode, a.ctx, a.label)
        if p is None:
            print("  FAILED to start"); rows.append({"mode": mode, "kind": "start_failed"}); continue
        load_s = round(time.time() - t0, 1)
        try:
            tok = lambda s: len(post("/tokenize", {"content": s})["tokens"])
            per = tok("".join(line(i) for i in range(2000))) / 2000
            head = f"Note: the vault passphrase is {NEEDLE}. Remember it.\n"
            n = 0
            for d in [int(x) for x in a.depths.split(",")]:
                body = head + "".join(line(i) for i in range(int(d / per)))
                q = "\n\nWhat is the vault passphrase mentioned at the top? Then write a 150-word summary of what kind of data these logs contain."
                t1 = time.time()
                r = post("/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": body + q}], "max_tokens": 220,
                                                  "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}})
                wall = time.time() - t1
                t = r["timings"]
                txt = r["choices"][0]["message"].get("content") or ""
                row = {"mode": mode, "kind": "depth", "target": d, "prompt_n": t["prompt_n"], "cache_n": t.get("cache_n", 0),
                       "prefill_tps": round(t["prompt_per_second"], 1), "prefill_s": round(t["prompt_ms"] / 1000, 1),
                       "decode_tps": round(t["predicted_per_second"], 2), "predicted_n": t["predicted_n"],
                       "accept": round(t["draft_n_accepted"] / t["draft_n"], 3) if t.get("draft_n") else None,
                       "needle_ok": NEEDLE in txt, "wall_s": round(wall, 1), "vram_mib": used_mib()}
                rows.append(row); print(" ", json.dumps(row), flush=True)
            rows.append({"mode": mode, "kind": "meta", "load_s": load_s})
        except Exception as e:
            print(f"  ERROR: {e}"); rows.append({"mode": mode, "kind": "error", "text": str(e)[:300]})
        finally:
            stop(p)
    with open(f"{OUT}/{a.label}-longctx-summary.json", "w") as f:
        json.dump(rows, f, indent=1)
    print(f"\n{'mode':<8}{'depth':>8}{'new tok':>9}{'prefill/s':>10}{'prefill s':>10}{'decode':>8}{'acc':>6}{'needle':>7}{'VRAM':>7}")
    for r in rows:
        if r["kind"] == "depth":
            print(f"{r['mode']:<8}{r['target']:>8}{r['prompt_n']:>9}{r['prefill_tps']:>10}{r['prefill_s']:>10}{r['decode_tps']:>8}{str(r['accept']):>6}{str(r['needle_ok']):>7}{r['vram_mib']:>7}")


if __name__ == "__main__":
    main()

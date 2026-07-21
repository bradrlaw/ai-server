#!/usr/bin/env python3
"""
--parallel sweep: find the concurrency setting that maximises aggregate throughput
per model, measured against the llama-swap router (:9090) with the engine's own
batching. For each (model, --parallel P) it reloads the model and runs a
concurrency sweep, recording aggregate tokens/sec + success rate.

Method notes:
- Edits config/llama-swap.yaml in place (always re-derived from a pristine backup,
  never compounded), setting the target model's --parallel and a high
  concurrencyLimit (llama-swap's default 10 otherwise 429s the sweep). llama-swap
  runs with -watch-config so it reloads on file change; we also force-unload +
  warm up to guarantee the new args take effect.
- --ctx-size is the TOTAL KV split across --parallel slots, so raising --parallel
  keeps VRAM ~constant (no OOM risk) while letting the GPU batch-decode P sequences.
- Restores the pristine config + rewarms daily models on exit.

Usage (run with the harness venv python):
  .venv/bin/python scripts/parallel-sweep.py --models coding \
      --parallels 1,4 --concs 1,4,8 --max-tokens 128            # smoke
  .venv/bin/python scripts/parallel-sweep.py --models coding,chat,fast \
      --parallels 1,2,4,8 --concs 1,2,4,8,12,16 --max-tokens 256  # full
"""
import argparse, asyncio, csv, os, re, subprocess, sys, time
from datetime import datetime
import aiohttp

CONFIG = "/srv/ai/config/llama-swap.yaml"
BACKUP = "/srv/ai/scratch/llama-swap.yaml.bak"

def ensure_backup():
    """Snapshot the current pristine config once so we always restore to it."""
    import os
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    if not os.path.exists(BACKUP):
        open(BACKUP, "w").write(open(CONFIG).read())
SWAP = "http://127.0.0.1:9090"
CHAT_URL = f"{SWAP}/v1/chat/completions"
RESULTS_DIR = "/srv/ai/benchmarks/llm-scaling-bench/results"
PROMPT = "write me a 1000 word essay on the history and future of artificial intelligence"

MODEL_GPU = {"coding": 1, "chat": 2, "fast": 0,
             "big": 1, "coder-next": 1, "gemma-31b": 1, "gemma-26b": 2}  # VRAM-log card


def edit_config(model: str, parallel: int, concurrency: int):
    """Re-derive config from pristine backup: set model's --parallel + concurrencyLimit."""
    lines = open(BACKUP).read().splitlines(keepends=True)
    out, i, n = [], 0, len(lines)
    key_re = re.compile(r'^  "([a-z0-9-]+)":\s*$')
    while i < n:
        line = lines[i]
        m = key_re.match(line)
        if m and m.group(1) == model:
            # collect this model's block until the next top-level model key
            block = [line]
            j = i + 1
            while j < n and not key_re.match(lines[j]):
                block.append(lines[j])
                j += 1
            # edit --parallel inside the block
            for k, bl in enumerate(block):
                if "--parallel" in bl:
                    block[k] = re.sub(r"--parallel\s+\d+", f"--parallel {parallel}", bl)
            # insert concurrencyLimit after the `name:` line (4-space indent)
            for k, bl in enumerate(block):
                if re.match(r"^    name:", bl):
                    block.insert(k + 1, f"    concurrencyLimit: {concurrency}\n")
                    break
            out.extend(block)
            i = j
        else:
            out.append(line)
            i += 1
    open(CONFIG, "w").write("".join(out))


def restore_config():
    open(CONFIG, "w").write(open(BACKUP).read())


def vram(gpu: int) -> str:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,power.draw,utilization.gpu",
             "--format=csv,noheader,nounits", "-i", str(gpu)],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
        return r.stdout.strip()
    except Exception as e:
        return f"?({e})"


def swap_unload():
    for path in ("/unload", "/api/unload"):
        try:
            subprocess.run(["curl", "-s", "-m", "10", SWAP + path],
                           capture_output=True, timeout=15)
        except Exception:
            pass


async def one_request(session, max_tokens):
    payload = {"model": None, "messages": [{"role": "user", "content": PROMPT}],
               "max_tokens": max_tokens, "temperature": 0.7}
    return payload  # placeholder replaced per-call


async def send(session, model, max_tokens):
    t0 = time.time()
    payload = {"model": model, "messages": [{"role": "user", "content": PROMPT}],
               "max_tokens": max_tokens, "temperature": 0.7}
    try:
        async with session.post(CHAT_URL, json=payload,
                                timeout=aiohttp.ClientTimeout(total=900)) as r:
            data = await r.json()
            dt = time.time() - t0
            if r.status == 200:
                tok = data.get("usage", {}).get("completion_tokens", 0)
                return (tok > 0, tok, dt)
            return (False, 0, dt)
    except Exception:
        return (False, 0, time.time() - t0)


async def warmup(model, timeout=600):
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as s:
        while time.time() < deadline:
            ok, tok, dt = await send(s, model, 1)
            if ok:
                return True
            await asyncio.sleep(3)
    return False


async def run_conc(model, conc, max_tokens):
    conn = aiohttp.TCPConnector(limit=max(conc, 64), force_close=True)
    t0 = time.time()
    async with aiohttp.ClientSession(connector=conn) as s:
        results = await asyncio.gather(*[send(s, model, max_tokens) for _ in range(conc)])
    wall = time.time() - t0
    succ = sum(1 for ok, _, _ in results if ok)
    toks = sum(t for ok, t, _ in results if ok)
    tps = toks / wall if wall else 0
    return {"tokens_per_second": tps, "successful": succ, "failed": conc - succ,
            "total_tokens": toks, "total_time": wall,
            "success_rate": 100.0 * succ / conc if conc else 0}


async def sweep(models, parallels, concs, max_tokens, out_csv):
    rows = []
    for model in models:
        gpu = MODEL_GPU.get(model, "?")
        for P in parallels:
            print(f"\n{'='*64}\n[{model}] --parallel {P}  (reloading...)", flush=True)
            edit_config(model, P, concurrency=max(concs) * 2 + 8)
            time.sleep(3)              # let -watch-config pick up the edit
            swap_unload()
            time.sleep(2)
            if not await warmup(model):
                print(f"[{model}] WARMUP FAILED at --parallel {P}; skipping", flush=True)
                continue
            print(f"[{model}] ready. VRAM idx{gpu}: {vram(gpu)} (used MiB, W, %util)", flush=True)
            for c in concs:
                r = await run_conc(model, c, max_tokens)
                r.update({"model": model, "parallel": P, "concurrent_users": c})
                rows.append(r)
                print(f"  P={P} conc={c:>2}: {r['tokens_per_second']:7.1f} tok/s  "
                      f"{r['successful']}/{c} ok  ({r['total_time']:.1f}s)", flush=True)
                await asyncio.sleep(1)
        # restore this model between models so idle cards revert
        restore_config(); time.sleep(2)
    # write CSV
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["model", "parallel", "concurrent_users",
                                          "tokens_per_second", "successful", "failed",
                                          "total_tokens", "total_time", "success_rate"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})
    return rows


def print_matrix(rows, concs):
    models = sorted({r["model"] for r in rows}, key=lambda m: ["coding","chat","fast"].index(m) if m in ["coding","chat","fast"] else 9)
    for model in models:
        print(f"\n### {model} — aggregate tokens/sec (rows=--parallel, cols=concurrent users)")
        header = "  P\\conc |" + "".join(f"{c:>8}" for c in concs)
        print(header); print("  " + "-" * (len(header)-2))
        Ps = sorted({r["parallel"] for r in rows if r["model"] == model})
        for P in Ps:
            cells = []
            for c in concs:
                v = next((r["tokens_per_second"] for r in rows
                          if r["model"]==model and r["parallel"]==P and r["concurrent_users"]==c), None)
                cells.append(f"{v:>8.1f}" if v is not None else f"{'-':>8}")
            # mark best
            best = max((r["tokens_per_second"] for r in rows if r["model"]==model and r["parallel"]==P), default=0)
            print(f"  {P:>6} |" + "".join(cells) + f"   peak={best:.1f}")
        # best overall
        best = max((r for r in rows if r["model"]==model), key=lambda r: r["tokens_per_second"])
        print(f"  >> best: --parallel {best['parallel']} @ conc {best['concurrent_users']} "
              f"= {best['tokens_per_second']:.1f} tok/s ({best['success_rate']:.0f}% ok)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="coding")
    ap.add_argument("--parallels", default="1,4")
    ap.add_argument("--concs", default="1,4,8")
    ap.add_argument("--max-tokens", type=int, default=128)
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    parallels = [int(x) for x in a.parallels.split(",")]
    concs = [int(x) for x in a.concs.split(",")]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = f"{RESULTS_DIR}/parallel_sweep_{ts}.csv"
    print(f"models={models} parallels={parallels} concs={concs} max_tokens={a.max_tokens}")
    print(f"CSV -> {out_csv}")
    ensure_backup()
    try:
        rows = asyncio.run(sweep(models, parallels, concs, a.max_tokens, out_csv))
        print_matrix(rows, concs)
        print(f"\nCSV: {out_csv}")
    finally:
        restore_config()
        print("[restore] pristine llama-swap.yaml restored.")


if __name__ == "__main__":
    main()

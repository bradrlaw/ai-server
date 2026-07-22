#!/usr/bin/env python3
"""
P100 MoE --parallel throughput sweep (standalone, isolated from llama-swap).

Unlike scripts/parallel-sweep.py (which edits the llama-swap roster on the
V100s), this benches a model pinned to the P100 (idx0, 16 GB) by launching a
*standalone* llama-server on a private port. The standalone server holds the
P100's VRAM for the whole run, so llama-swap can't re-warm `fast` onto idx0
mid-sweep (its load attempts simply OOM and retry). Daily models are restored
on exit via scripts/llama-swap-mode.py.

For each --parallel P it (re)launches the server with `--ctx-size CTX_TOTAL`
(the TOTAL KV, split across P slots -> KV VRAM ~flat, but per-slot compute
buffers grow, so tight cards OOM at high P) and runs a concurrency sweep,
recording aggregate tokens/sec, per-request latency, and success rate.

Usage (harness venv python):
  benchmarks/llm-scaling-bench/.venv/bin/python scripts/p100-moe-sweep.py \
      --model /srv/ai/models/gemma-4-26b-a4b/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf \
      --label gemma-26b --parallels 1,2,4,8 --concs 1,2,4,8,12,16 \
      --max-tokens 256 --ctx-total 8192
"""
import argparse, asyncio, csv, os, signal, subprocess, sys, time
from datetime import datetime
import aiohttp

LLAMA_SERVER = "/srv/ai/src/llama.cpp/build/bin/llama-server"
SWAP = "http://127.0.0.1:9090"
RESULTS_DIR = "/srv/ai/benchmarks/llm-scaling-bench/results"
PROMPT = "write me a 1000 word essay on the history and future of artificial intelligence"


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


def vram_used(gpu: int) -> int:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-i", str(gpu)],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return -1


def swap_unload_fast():
    """Ask llama-swap to release the P100 slot before we grab it."""
    for path in ("/unload?model=fast", "/unload?model=fast-uncensored", "/unload"):
        try:
            subprocess.run(["curl", "-s", "-m", "10", SWAP + path], capture_output=True, timeout=15)
        except Exception:
            pass


def launch_server(model, gpu, port, parallel, ctx_total, ubatch):
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(gpu)}
    cmd = [LLAMA_SERVER,
           "--model", model,
           "--ctx-size", str(ctx_total),
           "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
           "--parallel", str(parallel),
           "--batch-size", str(ubatch), "--ubatch-size", str(ubatch),
           "--host", "127.0.0.1", "--port", str(port),
           "--n-gpu-layers", "999", "--flash-attn", "on",
           "--cont-batching", "--jinja", "--metrics"]
    log = open(f"/tmp/p100-sweep-p{parallel}.log", "w")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                         preexec_fn=os.setsid)
    return p, log


def kill_server(p):
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):
            if p.poll() is not None:
                break
            time.sleep(0.5)
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass


async def send(session, url, max_tokens):
    t0 = time.time()
    payload = {"model": "p100", "messages": [{"role": "user", "content": PROMPT}],
               "max_tokens": max_tokens, "temperature": 0.7}
    try:
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=900)) as r:
            data = await r.json()
            dt = time.time() - t0
            if r.status == 200:
                tok = data.get("usage", {}).get("completion_tokens", 0)
                return (tok > 0, tok, dt)
            return (False, 0, dt)
    except Exception:
        return (False, 0, time.time() - t0)


async def wait_ready(url, health_url, port, timeout=240):
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as s:
        # first wait for /health 200
        while time.time() < deadline:
            try:
                async with s.get(health_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            await asyncio.sleep(2)
        else:
            return False
        # then one real generation
        ok, _, _ = await send(s, url, 8)
        return ok


async def run_conc(url, conc, max_tokens):
    conn = aiohttp.TCPConnector(limit=max(conc, 64), force_close=True)
    t0 = time.time()
    async with aiohttp.ClientSession(connector=conn) as s:
        results = await asyncio.gather(*[send(s, url, max_tokens) for _ in range(conc)])
    wall = time.time() - t0
    succ = [(t, dt) for ok, t, dt in results if ok]
    toks = sum(t for t, _ in succ)
    tps = toks / wall if wall else 0
    mean_lat = sum(dt for _, dt in succ) / len(succ) if succ else 0
    per_stream = sum(t / dt for t, dt in succ if dt) / len(succ) if succ else 0
    return {"tokens_per_second": tps, "successful": len(succ), "failed": conc - len(succ),
            "total_tokens": toks, "total_time": wall,
            "mean_latency_s": mean_lat, "per_stream_tps": per_stream,
            "success_rate": 100.0 * len(succ) / conc if conc else 0}


async def sweep(a):
    url = f"http://127.0.0.1:{a.port}/v1/chat/completions"
    health = f"http://127.0.0.1:{a.port}/health"
    rows = []
    server = None
    try:
        for P in a.parallels:
            print(f"\n{'='*64}\n[{a.label}] --parallel {P} (ctx_total {a.ctx_total})  launching on P100(idx{a.gpu})...", flush=True)
            swap_unload_fast()
            time.sleep(2)
            server, _ = launch_server(a.model, a.gpu, a.port, P, a.ctx_total, a.ubatch)
            ready = await wait_ready(url, health, a.port)
            if not ready:
                tail = subprocess.run(["tail", "-3", f"/tmp/p100-sweep-p{P}.log"],
                                      capture_output=True, text=True).stdout
                print(f"[{a.label}] LOAD FAILED at --parallel {P} (likely OOM). Log tail:\n{tail}", flush=True)
                kill_server(server); server = None
                time.sleep(3)
                continue
            print(f"[{a.label}] ready. VRAM idx{a.gpu}: {vram(a.gpu)} (used MiB, W, %util)", flush=True)
            peak = vram_used(a.gpu)
            for c in a.concs:
                r = await run_conc(url, c, a.max_tokens)
                peak = max(peak, vram_used(a.gpu))
                r.update({"model": a.label, "parallel": P, "concurrent_users": c, "peak_vram_mib": peak})
                rows.append(r)
                print(f"  P={P} conc={c:>2}: {r['tokens_per_second']:7.1f} tok/s agg | "
                      f"{r['per_stream_tps']:5.1f} tok/s/stream | lat {r['mean_latency_s']:5.1f}s | "
                      f"{r['successful']}/{c} ok", flush=True)
                await asyncio.sleep(1)
            kill_server(server); server = None
            time.sleep(4)  # let VRAM drain before next P
    finally:
        if server:
            kill_server(server)
    return rows


def print_matrix(rows, concs, label):
    print(f"\n### {label} on P100 — aggregate tokens/sec (rows=--parallel, cols=concurrent users)")
    Ps = sorted({r["parallel"] for r in rows})
    header = "  P\\conc |" + "".join(f"{c:>8}" for c in concs)
    print(header); print("  " + "-" * (len(header) - 2))
    for P in Ps:
        cells = []
        for c in concs:
            v = next((r["tokens_per_second"] for r in rows
                      if r["parallel"] == P and r["concurrent_users"] == c), None)
            cells.append(f"{v:>8.1f}" if v is not None else f"{'-':>8}")
        best = max((r["tokens_per_second"] for r in rows if r["parallel"] == P), default=0)
        print(f"  {P:>6} |" + "".join(cells) + f"   peak={best:.1f}")
    if rows:
        best = max(rows, key=lambda r: r["tokens_per_second"])
        print(f"  >> best: --parallel {best['parallel']} @ conc {best['concurrent_users']} "
              f"= {best['tokens_per_second']:.1f} tok/s ({best['success_rate']:.0f}% ok, "
              f"peak {best['peak_vram_mib']} MiB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", default="p100-moe")
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--port", type=int, default=10099)
    ap.add_argument("--parallels", default="1,2,4,8")
    ap.add_argument("--concs", default="1,2,4,8,12,16")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--ctx-total", type=int, default=8192)
    ap.add_argument("--ubatch", type=int, default=512)
    ap.add_argument("--no-restore", action="store_true")
    a = ap.parse_args()
    a.parallels = [int(x) for x in a.parallels.split(",")]
    a.concs = [int(x) for x in a.concs.split(",")]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_csv = f"{RESULTS_DIR}/p100_moe_sweep_{a.label}_{ts}.csv"
    print(f"model={a.model}\nlabel={a.label} gpu={a.gpu} parallels={a.parallels} "
          f"concs={a.concs} max_tokens={a.max_tokens} ctx_total={a.ctx_total}")
    print(f"CSV -> {out_csv}")
    try:
        rows = asyncio.run(sweep(a))
        if rows:
            with open(out_csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["model", "parallel", "concurrent_users",
                                                  "tokens_per_second", "per_stream_tps",
                                                  "mean_latency_s", "successful", "failed",
                                                  "total_tokens", "total_time", "success_rate",
                                                  "peak_vram_mib"])
                w.writeheader()
                for r in rows:
                    w.writerow({k: r[k] for k in w.fieldnames})
            print_matrix(rows, a.concs, a.label)
            print(f"\nCSV: {out_csv}")
    finally:
        if not a.no_restore:
            print("[restore] rewarming daily models (llama-swap-mode set daily)...")
            subprocess.run(["python3", "/srv/ai/scripts/llama-swap-mode.py", "set", "daily"],
                           capture_output=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Load/behaviour check for the on-demand dual-V100 slots (big, coder-next) via llama-swap :9090.

Per model: cold-load time (first tiny request), VRAM per GPU, single-stream decode on a code task,
a ~60k-token needle prompt (prefill speed + long-context correctness). Throwaway, untracked.

  bigcn.py big coder-next
"""
import json, subprocess, sys, time, importlib.util
spec = importlib.util.spec_from_file_location("soak", "/srv/ai/tmp/lcpp-test/soak.py")
soak = importlib.util.module_from_spec(spec); spec.loader.exec_module(soak)


def vram():
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return {l.split(",")[0].strip(): int(l.split(",")[1]) for l in out.strip().splitlines()}


def timing(resp):
    t = resp.get("timings", {})
    return round(t.get("predicted_per_second", 0), 1), round(t.get("prompt_per_second", 0), 1)


for model in sys.argv[1:]:
    print(f"== {model}", flush=True)
    t0 = time.time()
    s, r, dt = soak.post(model, [{"role": "user", "content": "Reply with only the word pong."}], 800, timeout=1800)
    print(f"  cold load + first reply: {time.time() - t0:.0f}s status={s} "
          f"reply={(r['choices'][0]['message'].get('content') or '')[:20]!r}" if s == 200 else f"  FAILED {s} {r}", flush=True)
    print("  VRAM MiB:", vram(), flush=True)
    s, r, dt = soak.post(model, [{"role": "user", "content": "Write a Python function merge_intervals(intervals) that merges "
                                  "overlapping [start, end] intervals, with a docstring and three asserts."}], 3000, timeout=900)
    if s == 200:
        d, p = timing(r)
        print(f"  code task: {dt:.0f}s gen={r['usage']['completion_tokens']} decode={d} t/s (prefill {p})", flush=True)
    else:
        print("  code task FAILED", s, r, flush=True)
    text = soak.corpus(200000, 11)
    msgs = [{"role": "user", "content": text + "\n\nQuestion: what is the maintenance code word for this document set? Answer with just the code word."}]
    s, r, dt = soak.post(model, msgs, 2000, temperature=0.2, timeout=1800)
    if s == 200:
        d, p = timing(r)
        c = r["choices"][0]["message"].get("content") or ""
        print(f"  needle ({r['usage']['prompt_tokens']} tok): {dt:.0f}s prefill={p} t/s decode={d} found={soak.NEEDLE in c} "
              f"finish={r['choices'][0]['finish_reason']}", flush=True)
    else:
        print("  needle FAILED", s, r, flush=True)
    print("  VRAM MiB after:", vram(), flush=True)

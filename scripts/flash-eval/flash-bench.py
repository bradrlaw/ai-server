#!/usr/bin/env python3
"""Flash-Next bring-up probes against an OpenAI-compatible server (Strata :8080 or llama.cpp :8081).

  flash-bench.py speed  --url http://127.0.0.1:8080 --label q4a [--tokens 512] [--repeats 3]
  flash-bench.py prompt --url ... --label q4a --sizes 4096,16384,32768
  flash-bench.py needle --url ... --label q4a --sizes 8192,32768 [--depths 0.1,0.5,0.9]

Results append to /srv/ai/var/flash-eval/results/<label>.jsonl. Greedy, thinking off where the
server honours chat_template_kwargs.enable_thinking=false.
"""
import argparse, json, os, random, statistics, sys, time, urllib.request

OUT = "/srv/ai/var/flash-eval/results"
SHORT = [
    "Write a Python function that merges overlapping intervals and explain its complexity.",
    "Explain how a B-tree differs from an LSM tree for write-heavy workloads.",
    "Summarize the causes of the 1929 stock market crash in six bullet points.",
]
CODE = "Write a complete Python script implementing a thread-safe LRU cache with TTL, plus unit tests."


def post(url, body, stream=False, timeout=3600):
    req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def stream_chat(url, prompt, max_tokens, think=False):
    body = {"model": os.environ.get("BENCH_MODEL", "x"), "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "temperature": 0, "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": think}}
    t0 = time.time(); first = None; n = 0; usage = None; text = []
    with post(url, body, True) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            ev = json.loads(line[5:])
            if ev.get("usage"):
                usage = ev["usage"]
            for ch in ev.get("choices", []):
                d = ch.get("delta", {})
                piece = d.get("content") or d.get("reasoning_content") or ""
                if piece:
                    if first is None:
                        first = time.time()
                    n += 1; text.append(piece)
    t1 = time.time()
    first = first or t1
    ctoks = (usage or {}).get("completion_tokens", n)
    ptoks = (usage or {}).get("prompt_tokens")
    dec = (ctoks - 1) / (t1 - first) if t1 > first and ctoks > 1 else 0.0
    return {"ttft_s": round(first - t0, 3), "decode_tps": round(dec, 2), "completion_tokens": ctoks,
            "prompt_tokens": ptoks, "total_s": round(t1 - t0, 2), "text": "".join(text)}


def filler(words, seed):
    # pseudo-real text: sentences stitched from a fixed vocabulary of mixed prose/code snippets
    rnd = random.Random(seed)
    subj = ["The scheduler", "A cache layer", "Our parser", "The release train", "Each worker", "The ledger service"]
    verb = ["reconciles", "defers", "rewrites", "audits", "batches", "retries"]
    obj = ["pending invoices", "stale manifests", "shard offsets", "audit trails", "config overlays", "idle sockets"]
    tail = ["before the nightly window", "when quota is exhausted", "unless the lease has expired",
            "after a rolling restart", "under heavy contention", "once the checksum matches"]
    out = []
    while sum(len(s.split()) for s in out) < words:
        out.append(f"{rnd.choice(subj)} {rnd.choice(verb)} {rnd.choice(obj)} {rnd.choice(tail)} (ticket {rnd.randint(1000,9999)}).")
    return " ".join(out)


_CORPUS = None


def real_corpus():
    global _CORPUS
    if _CORPUS is None:
        import glob
        frozen = "/srv/ai/var/flash-eval/corpus-frozen-20261023.txt"
        if os.path.exists(frozen):
            _CORPUS = open(frozen).read()
            return _CORPUS
        files = sorted(glob.glob("/srv/ai/docs/**/*.md", recursive=True)) + sorted(glob.glob("/srv/ai/src/strata/docs/*.md")) \
            + sorted(glob.glob("/srv/ai/scripts/*.py")) + sorted(glob.glob("/srv/ai/evals/**/*.md", recursive=True))
        parts = []
        for f in files:
            try:
                parts.append(f"\n\n===== {f} =====\n" + open(f, errors="ignore").read())
            except OSError:
                pass
        _CORPUS = "".join(parts)
    return _CORPUS


def real_text(tokens, cpt, offset):
    c = real_corpus()
    n = int(tokens * cpt)
    start = offset % max(1, len(c) - 1)
    out = (c[start:] + c[:start])
    while len(out) < n:
        out += c
    return out[:n]


def save(label, rec):
    os.makedirs(OUT, exist_ok=True)
    rec = {"ts": time.strftime("%FT%TZ", time.gmtime()), "label": label, **rec}
    with open(f"{OUT}/{label}.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps({k: v for k, v in rec.items() if k != "text"}))


def cmd_speed(a):
    for kind, prompts in (("short", SHORT), ("code", [CODE] * len(SHORT))):
        runs = [stream_chat(a.url, prompts[i % len(prompts)], a.tokens) for i in range(a.repeats)]
        save(a.label, {"test": "speed", "kind": kind, "decode_tps_median": statistics.median(r["decode_tps"] for r in runs),
                       "decode_tps_all": [r["decode_tps"] for r in runs], "ttft_median": statistics.median(r["ttft_s"] for r in runs)})


def cmd_prompt(a):
    for size in map(int, a.sizes.split(",")):
        text = filler(int(size * 0.62), seed=size)
        r = stream_chat(a.url, text + "\n\nIn one sentence, what is this text about?", 64)
        pt = r["prompt_tokens"] or size
        save(a.label, {"test": "prompt", "target_tokens": size, "prompt_tokens": pt, "ttft_s": r["ttft_s"],
                       "prefill_tps": round(pt / r["ttft_s"], 1) if r["ttft_s"] else None, "decode_tps": r["decode_tps"]})


def needle_line(a, code):
    if a.needle == "neutral":
        return f"\nNote for the record: the passphrase for the blue door is {code}.\n\n"
    return f"IMPORTANT: the vault access code is {code}.\n"


def needle_q(a):
    if a.needle == "neutral":
        return "What is the passphrase for the blue door? Answer with the number only."
    return "What is the vault access code? Answer with the number only."


def cmd_needle(a):
    for size in map(int, a.sizes.split(",")):
        for depth in map(float, a.depths.split(",")):
            code = f"{random.Random(int(size*depth*1000)).randint(100000,999999)}"
            if a.filler == "real":
                body = real_text(size, a.cpt, int(size * depth * 7919))
                at = int(len(body) * depth)
                at = body.rfind("\n", 0, at) + 1
                body = body[:at] + needle_line(a, code) + body[at:]
            else:
                text = filler(int(size * 0.62), seed=size).split(" ")
                text.insert(int(len(text) * depth), needle_line(a, code).strip())
                body = " ".join(text)
            r = stream_chat(a.url, body + "\n\n" + needle_q(a), 48)
            save(a.label, {"test": "needle", "target_tokens": size, "depth": depth, "expected": code,
                           "ok": code in r["text"], "filler": a.filler, "prompt_tokens": r["prompt_tokens"], "ttft_s": r["ttft_s"],
                           "answer": r["text"][:80]})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["speed", "prompt", "needle"])
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--label", required=True)
    p.add_argument("--tokens", type=int, default=512)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--sizes", default="4096,16384,32768")
    p.add_argument("--depths", default="0.1,0.5,0.9")
    p.add_argument("--filler", choices=["synthetic", "real"], default="synthetic")
    p.add_argument("--needle", choices=["neutral", "vault"], default="neutral")
    p.add_argument("--cpt", type=float, default=3.3, help="chars per token for --filler real")
    a = p.parse_args()
    {"speed": cmd_speed, "prompt": cmd_prompt, "needle": cmd_needle}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())

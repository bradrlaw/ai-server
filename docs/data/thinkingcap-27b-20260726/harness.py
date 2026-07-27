#!/usr/bin/env python3
"""Matched thinking-token + accuracy harness (multi-sample).
Hits an OpenAI-compatible chat endpoint (llama-server) over a fixed GSM8K item set,
K samples per item at a given sampler, thinking ON. Records per-gen correctness,
total completion tokens, and reasoning-token count (tokenized reasoning_content).
Env: TEMP TOP_P TOP_K K  (sampler + samples-per-item).
Usage: harness.py <label> <host:port> <items.json> <out_prefix> [concurrency]
"""
import sys, os, json, re, statistics, urllib.request
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor

label, hostport, items_path, out_prefix = sys.argv[1:5]
CONC = int(sys.argv[5]) if len(sys.argv) > 5 else 4
TEMP = float(os.environ.get("TEMP", "1.0"))
TOP_P = float(os.environ.get("TOP_P", "0.95"))
TOP_K = int(os.environ.get("TOP_K", "20"))
K = int(os.environ.get("K", "1"))
BASE = f"http://{hostport}"
items = json.load(open(items_path))


def post(path, payload, timeout=1800):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ntok(text):
    if not text:
        return 0
    try:
        return len(post("/tokenize", {"content": text}).get("tokens", []))
    except Exception:
        return -1


NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def extract(txt):
    if not txt:
        return None
    nums = NUM.findall(txt)
    return nums[-1].replace(",", "").rstrip(".") if nums else None


def grade(pred, gold):
    if pred is None:
        return False
    try:
        return abs(float(pred) - float(gold)) < 1e-4
    except Exception:
        return str(pred).strip() == str(gold).strip()


def run_one(args):
    qi, seed, it = args
    payload = {"temperature": TEMP, "top_p": TOP_P, "top_k": TOP_K, "seed": seed,
               "max_tokens": 8000,
               "messages": [{"role": "user", "content": it["q"]}]}
    try:
        d = post("/v1/chat/completions", payload)
    except Exception as e:
        return {"qi": qi, "ok": False, "err": str(e)[:120]}
    m = d["choices"][0]["message"]
    u = d.get("usage", {})
    content = m.get("content") or ""
    pred = extract(content)
    return {"qi": qi, "ok": True, "correct": grade(pred, it["gold"]),
            "pred": pred, "gold": it["gold"],
            "completion_tokens": u.get("completion_tokens"),
            "reasoning_tokens": ntok(m.get("reasoning_content") or ""),
            "answer_tokens": ntok(content),
            "finish": d["choices"][0].get("finish_reason")}


tasks = [(qi, 1000 + s, it) for qi, it in enumerate(items) for s in range(K)]
print(f"### {label}: {len(items)} items x{K} = {len(tasks)} gens | "
      f"temp={TEMP} top_p={TOP_P} top_k={TOP_K} conc={CONC}", flush=True)
results = [None] * len(tasks)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    futs = {ex.submit(run_one, t): i for i, t in enumerate(tasks)}
    done = 0
    for f in list(futs):
        results[futs[f]] = f.result()
        done += 1
        if done % 20 == 0:
            print(f"  {done}/{len(tasks)} done", flush=True)

ok = [r for r in results if r and r.get("ok")]
with open(out_prefix + ".jsonl", "w") as fh:
    for r in results:
        fh.write(json.dumps(r) + "\n")


def stat(k):
    vals = [r[k] for r in ok if isinstance(r.get(k), (int, float))
            and not isinstance(r.get(k), bool) and r[k] >= 0]
    if not vals:
        return {"mean": 0, "std": 0}
    return {"mean": round(statistics.mean(vals), 1),
            "std": round(statistics.pstdev(vals), 1) if len(vals) > 1 else 0.0}


by_q = defaultdict(list)
for r in ok:
    by_q[r["qi"]].append(r)
maj_correct = 0
for qi, rs in by_q.items():
    preds = [r["pred"] for r in rs if r["pred"] is not None]
    if not preds:
        continue
    top = Counter(preds).most_common(1)[0][0]
    if grade(top, rs[0]["gold"]):
        maj_correct += 1

n = len(ok)
summary = {
    "label": label, "items": len(items), "K": K, "n_gens": n,
    "sampler": {"temp": TEMP, "top_p": TOP_P, "top_k": TOP_K},
    "accuracy_avg": round(sum(r["correct"] for r in ok) / n, 4) if n else 0,
    "accuracy_majority": round(maj_correct / len(by_q), 4) if by_q else 0,
    "reasoning_tokens": stat("reasoning_tokens"),
    "completion_tokens": stat("completion_tokens"),
    "answer_tokens": stat("answer_tokens"),
    "truncated_length": sum(1 for r in ok if r.get("finish") == "length"),
    "errors": sum(1 for r in results if r and not r.get("ok")),
}
json.dump(summary, open(out_prefix + ".summary.json", "w"), indent=2)
print("### SUMMARY:", json.dumps(summary), flush=True)

#!/usr/bin/env python3
"""Mixed-load soak for the daily trio via llama-swap :9090 (throwaway, untracked).

One worker thread per slot (coding / chat / small) loops requests for --minutes. The coding
worker sends a ~100k-token needle-in-a-haystack prompt once at --long-at minutes, and the chat
worker a ~30k-token one. A sampler records nvidia-smi every 15 s. Every reply is graded
(HTTP 200, non-empty content, needle found, code requests contain `def`), and per-request
decode / prefill t/s come from llama.cpp's `timings`.

  soak.py --minutes 30 --long-at 10 --tag B
"""
import argparse, glob, json, os, random, subprocess, threading, time, urllib.error, urllib.request

URL = "http://127.0.0.1:9090/v1/chat/completions"
DOCS = sorted(glob.glob("/srv/ai/docs/*.md") + glob.glob("/srv/ai/docs/adr/*.md"))
NEEDLE = "TANGERINE-4471"
STOP = threading.Event()
LOCK = threading.Lock()
ROWS = []


def corpus(chars, seed):
    rnd = random.Random(seed)
    parts, n = [], 0
    files = DOCS[:]
    rnd.shuffle(files)
    for f in files:
        t = open(f, encoding="utf-8", errors="replace").read()
        parts.append(f"\n\n===== {os.path.basename(f)} =====\n{t}")
        n += len(t)
        if n >= chars:
            break
    text = "".join(parts)[:chars]
    mid = len(text) // 2
    cut = text.rfind("\n", 0, mid)
    return text[:cut] + f"\n\nNOTE FROM THE OPERATOR: the maintenance code word for this document set is {NEEDLE}.\n\n" + text[cut:]


def post(model, messages, max_tokens, temperature=0.6, timeout=1500):
    body = {"model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "top_p": 0.95, "top_k": 20}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}, time.time() - t0
    except Exception as e:  # noqa: BLE001 - any transport failure is a failed request
        return 0, {"error": repr(e)[:300]}, time.time() - t0


CODE_TASKS = [
    "Write a Python function `merge_intervals(intervals)` that merges overlapping [start, end] intervals. Include a short docstring and three asserts.",
    "Write a Python class `LRUCache(capacity)` with get/put in O(1) using OrderedDict. Include a tiny usage example.",
    "Write a Python function `parse_duration(s)` that turns strings like '1h30m15s' into seconds and raises ValueError on bad input.",
    "Write a bash script that rotates log files in a directory, keeping the newest 5 per prefix. Explain each step briefly.",
    "Write a Python function `top_k_words(text, k)` returning the k most common lowercase words, ties broken alphabetically.",
]


def record(tag, model, kind, status, resp, dt, why):
    t = resp.get("timings", {}) if status == 200 else {}
    u = resp.get("usage", {}) if status == 200 else {}
    row = {"t": round(time.time(), 1), "model": model, "kind": kind, "status": status, "ok": why is None,
           "why": why, "sec": round(dt, 1), "prompt_tok": u.get("prompt_tokens"), "gen_tok": u.get("completion_tokens"),
           "decode_tps": round(t.get("predicted_per_second", 0), 1) or None,
           "prefill_tps": round(t.get("prompt_per_second", 0), 1) or None}
    with LOCK:
        ROWS.append(row)
        print(f"{time.strftime('%H:%M:%S')} {model:6s} {kind:10s} {'ok ' if why is None else 'FAIL'} {dt:6.1f}s "
              f"p={row['prompt_tok']} g={row['gen_tok']} dec={row['decode_tps']} pre={row['prefill_tps']} {why or ''}", flush=True)


def grade(resp, status, need=None, want_def=False):
    if status != 200:
        return f"HTTP {status}: {str(resp.get('error', ''))[:120]}"
    ch = resp["choices"][0]
    content = ch["message"].get("content") or ""
    if not content.strip():
        return f"empty content (finish={ch.get('finish_reason')})"
    if need and need not in content:
        return f"needle {need} missing from answer"
    if want_def and "def " not in content and "#!/" not in content:
        return "no code in answer"
    return None


def worker(model, tag, deadline, long_at, long_chars, period):
    rnd = random.Random(hash(model) & 0xFFFF)
    did_long = False
    i = 0
    while not STOP.is_set() and time.time() < deadline:
        elapsed_min = (time.time() - T0) / 60
        if long_chars and not did_long and elapsed_min >= long_at:
            did_long = True
            text = corpus(long_chars, 7)
            msgs = [{"role": "user", "content": text + "\n\nQuestion: what is the maintenance code word for this document set? "
                                                      "Answer with just the code word."}]
            s, r, dt = post(model, msgs, 1500, temperature=0.2)
            record(tag, model, "long-needle", s, r, dt, grade(r, s, need=NEEDLE))
            continue
        kind = ["code", "summary", "qa"][i % 3]
        if kind == "code":
            msgs = [{"role": "user", "content": rnd.choice(CODE_TASKS)}]
            s, r, dt = post(model, msgs, 3000)
            record(tag, model, kind, s, r, dt, grade(r, s, want_def=True))
        elif kind == "summary":
            text = corpus(24000, rnd.randrange(10**6))
            msgs = [{"role": "user", "content": text + "\n\nSummarise the documents above in five bullet points."}]
            s, r, dt = post(model, msgs, 1200)
            record(tag, model, kind, s, r, dt, grade(r, s))
        else:
            msgs = [{"role": "user", "content": rnd.choice([
                "Explain the difference between a mutex and a semaphore in four sentences.",
                "Why might a CUDA kernel be memory-bound rather than compute-bound? Answer in a short paragraph.",
                "Give me three tips for writing a good commit message."])}]
            s, r, dt = post(model, msgs, 1000)
            record(tag, model, kind, s, r, dt, grade(r, s))
        i += 1
        time.sleep(period)


def sampler(path, deadline):
    with open(path, "w") as f:
        f.write("ts,idx,mem_mib,temp_c,power_w,util_pct,sm_mhz\n")
        while not STOP.is_set() and time.time() < deadline + 120:
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used,temperature.gpu,power.draw,utilization.gpu,clocks.sm",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15,
                                     env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}).stdout
                ts = int(time.time())
                for line in out.strip().splitlines():
                    f.write(f"{ts},{line.replace(' ', '')}\n")
                f.flush()
            except Exception:  # noqa: BLE001 - sampling is best effort
                pass
            time.sleep(15)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--long-at", type=float, default=10, help="minute at which the long prompts are sent")
    ap.add_argument("--tag", default="B")
    ap.add_argument("--out", default="/srv/ai/tmp/lcpp-test")
    a = ap.parse_args()
    T0 = time.time()
    deadline = T0 + a.minutes * 60
    gpu_path = f"{a.out}/soak-{a.tag}-gpu.csv"
    threads = [threading.Thread(target=sampler, args=(gpu_path, deadline), daemon=True)]
    # (model, long prompt chars (~3.4 chars/token), pause between requests)
    for model, long_chars, period in (("coding", 340000, 1), ("chat", 100000, 1), ("small", 0, 1)):
        threads.append(threading.Thread(target=worker, args=(model, a.tag, deadline, a.long_at, long_chars, period)))
    for t in threads:
        t.start()
    for t in threads[1:]:
        t.join()
    STOP.set()
    with open(f"{a.out}/soak-{a.tag}.json", "w") as f:
        json.dump(ROWS, f, indent=1)
    print("\nmodel   n   ok  fail  mean_s  mean_decode_tps")
    for m in ("coding", "chat", "small"):
        rs = [r for r in ROWS if r["model"] == m]
        dec = [r["decode_tps"] for r in rs if r["decode_tps"] and r["kind"] != "long-needle"]
        print(f"{m:7s}{len(rs):3d} {sum(r['ok'] for r in rs):4d} {sum(not r['ok'] for r in rs):5d} "
              f"{sum(r['sec'] for r in rs) / max(len(rs), 1):7.1f} {sum(dec) / max(len(dec), 1):8.1f}")
    for r in ROWS:
        if not r["ok"]:
            print("FAIL", r["model"], r["kind"], r["why"])

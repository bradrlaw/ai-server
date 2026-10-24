#!/usr/bin/env python3
"""strata-soak.py <url> <model> <minutes> <out.jsonl>: 2 workers, mixed chat/code/16K/100K-needle load."""
import json, random, sys, threading, time, urllib.request
url, model, minutes, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
CORPUS = open("/srv/ai/var/flash-eval/corpus-frozen-20261023.txt", errors="replace").read()
STOP = threading.Event(); LOCK = threading.Lock(); T0 = time.time(); long_sent = [False]
def doc(chars, seed):
    r = random.Random(seed); o = r.randrange(0, max(1, len(CORPUS) - chars)); t = CORPUS[o:o + chars]
    k = r.randrange(0, len(t)); return t[:k] + "\nThe passphrase for the blue door is TANGERINE-4471.\n" + t[k:]
def call(body, tmo=1500):
    t = time.time()
    try:
        r = urllib.request.Request(url, json.dumps({"model": model, **body}).encode(), {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(r, timeout=tmo)); m = d["choices"][0]["message"]
        return True, (m.get("content") or ""), d.get("usage", {}), time.time() - t, d["choices"][0].get("finish_reason")
    except Exception as e: return False, str(e)[:200], {}, time.time() - t, None
def work(wid):
    r = random.Random(wid); n = 0
    while not STOP.is_set():
        n += 1; kind = r.choice(["short", "code", "doc16k", "short", "code"])
        if wid == 0 and not long_sent[0] and time.time() - T0 > minutes * 60 / 3: long_sent[0] = True; kind = "doc100k"
        nt = {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0.7}
        if kind == "short": b = {"messages": [{"role": "user", "content": r.choice(["Explain TCP slow start.", "Why do cats purr?", "Summarize the French Revolution in 5 sentences."])}], "max_tokens": 300, **nt}; chk = lambda t: len(t) > 20
        elif kind == "code": b = {"messages": [{"role": "user", "content": r.choice(["Write a Python LRU cache class with tests.", "Write a Go HTTP server with graceful shutdown.", "Write a JS function to deep clone an object."])}], "max_tokens": 700, **nt}; chk = lambda t: len(t) > 100
        else:
            ch = 48000 if kind == "doc16k" else 270000
            b = {"messages": [{"role": "user", "content": doc(ch, n * 7 + wid) + "\n\nWhat is the passphrase for the blue door? Answer with the passphrase only."}], "max_tokens": 60, **nt, "temperature": 0}; chk = lambda t: "TANGERINE-4471" in t
        ok, txt, u, dt, fin = call(b)
        row = {"t": round(time.time() - T0), "w": wid, "kind": kind, "http_ok": ok, "pass": bool(ok and chk(txt)), "sec": round(dt, 1),
               "ptok": u.get("prompt_tokens"), "ctok": u.get("completion_tokens"), "finish": fin, "err": None if ok else txt}
        with LOCK: open(out, "a").write(json.dumps(row) + "\n")
ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
[t.start() for t in ts]; time.sleep(minutes * 60); STOP.set(); [t.join(timeout=1800) for t in ts]

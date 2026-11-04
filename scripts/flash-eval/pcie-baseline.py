#!/usr/bin/env python3
"""PCIe link baseline for Flash-Next (Strata `flash`): run before and after moving the V100s to x16.

  pcie-baseline.py run     --label x8  [--sizes 32768,131072,262144] [--repeats 2] [--force]
  pcie-baseline.py compare --a x8 --b x16

Per run it (1) unloads `flash` and times a cold reload through llama-swap, (2) sends cold prompts
of each size and records TTFT / prefill tok/s, (3) measures decode tok/s as a control, and
samples `nvidia-smi dmon` rx/tx (MB/s) plus link gen/width at 1 s during every phase.

Disruptive: unloads `flash` (any session using it fails) and the cold prompts evict its
conversation cache. Do not run while a Copilot/Hermes session is active; it refuses if
`flash` is busy unless --force. Link width is only meaningful under load (this run provides it).
After the run, `flash` is loaded; run `scripts/llama-swap-mode.py set daily` to restore the trio.

Results append to /srv/ai/var/flash-eval/results/pcie-<label>.jsonl (same dir as flash-bench.py).
"""
import argparse, importlib.util, json, os, statistics, subprocess, sys, threading, time, urllib.request

SWAP = "http://127.0.0.1:9090"
STRATA = "http://127.0.0.1:10006"
OUT = "/srv/ai/var/flash-eval/results"
V100 = [int(x) for x in os.environ.get("V100_IDX", "1,2").split(",")]
HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("flash_bench", os.path.join(HERE, "flash-bench.py"))
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)
os.environ["BENCH_MODEL"] = "flash"


def http(url, method="GET", timeout=30):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    return urllib.request.urlopen(req, timeout=timeout).read()


def running():
    return [m["model"] for m in json.loads(http(SWAP + "/running")).get("running", []) if m.get("state") == "ready"]


class Sampler:
    """1 Hz rx/tx (MB/s) per GPU from `nvidia-smi dmon`, plus link gen/width."""

    def __init__(self):
        self.rows = []      # (t, idx, rx, tx)
        self.links = {}     # idx -> (gen, width) highest width seen
        self.procs = []
        env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID")
        dmon = subprocess.Popen(["nvidia-smi", "dmon", "-s", "t", "-d", "1"], stdout=subprocess.PIPE,
                                text=True, env=env, bufsize=1)
        link = subprocess.Popen(["nvidia-smi", "--query-gpu=index,pcie.link.gen.current,pcie.link.width.current",
                                 "--format=csv,noheader,nounits", "-l", "1"], stdout=subprocess.PIPE, text=True, bufsize=1)
        self.procs = [dmon, link]
        threading.Thread(target=self._dmon, args=(dmon,), daemon=True).start()
        threading.Thread(target=self._link, args=(link,), daemon=True).start()

    def _dmon(self, p):
        for line in p.stdout:
            f = line.split()
            if len(f) == 3 and f[0].isdigit():
                try:
                    self.rows.append((time.time(), int(f[0]), float(f[1]), float(f[2])))
                except ValueError:
                    pass

    def _link(self, p):
        for line in p.stdout:
            f = [x.strip() for x in line.split(",")]
            if len(f) == 3 and f[0].isdigit():
                idx, gen, w = int(f[0]), int(f[1]), int(f[2])
                g0, w0 = self.links.get(idx, (0, 0))
                self.links[idx] = (max(g0, gen), max(w0, w))

    def window(self, t0, t1):
        out = {}
        for idx in V100:
            rx = [r for (t, i, r, _) in self.rows if i == idx and t0 <= t <= t1 + 1]
            tx = [x for (t, i, _, x) in self.rows if i == idx and t0 <= t <= t1 + 1]
            out[f"gpu{idx}"] = {"rx_peak_mbs": max(rx, default=0), "rx_mean_mbs": round(statistics.fmean(rx), 0) if rx else 0,
                                "tx_peak_mbs": max(tx, default=0)}
        return out

    def stop(self):
        for p in self.procs:
            p.terminate()


def save(label, rec):
    os.makedirs(OUT, exist_ok=True)
    rec = {"ts": time.strftime("%FT%TZ", time.gmtime()), "label": f"pcie-{label}", **rec}
    with open(f"{OUT}/pcie-{label}.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))
    return rec


def unload_flash():
    for path in ("/api/models/unload/flash", "/api/models/unload"):
        try:
            http(SWAP + path, "POST", 120)
        except Exception as e:
            print(f"  {path}: {e}")
            continue
        for _ in range(60):
            if "flash" not in running():
                return path
            time.sleep(2)
    sys.exit("could not unload flash")


def cmd_run(a):
    if "flash" in running():
        try:
            live = json.loads(http(STRATA + "/metrics")).get("live", {})
            if (live.get("state") != "idle" or live.get("queued")) and not a.force:
                sys.exit(f"flash is busy ({live.get('state')}, queued {live.get('queued')}); stop the session or use --force")
        except Exception as e:
            print(f"  (could not read Strata metrics: {e})")
    smp = Sampler()
    time.sleep(3)
    try:
        print("== cold load ==")
        if "flash" in running():
            print(f"  unloaded via {unload_flash()}")
        t0 = time.time()
        r = fb.stream_chat(SWAP, "Reply with the single word: ready.", 8)
        t1 = time.time()
        save(a.label, {"test": "load", "load_s": round(t1 - t0, 1), "window": smp.window(t0, t1)})

        print("== decode control ==")
        runs = [fb.stream_chat(SWAP, fb.SHORT[i % 3], 512) for i in range(3)]
        save(a.label, {"test": "decode", "decode_tps_median": statistics.median(x["decode_tps"] for x in runs),
                       "decode_tps_all": [x["decode_tps"] for x in runs]})

        for size in map(int, a.sizes.split(",")):
            for rep in range(a.repeats):
                text = fb.filler(int(size * 0.62), seed=size * 10 + rep + 1)
                t0 = time.time()
                r = fb.stream_chat(SWAP, text + "\n\nIn one sentence, what is this text about?", 64)
                t1 = time.time()
                pt = r["prompt_tokens"] or size
                save(a.label, {"test": "cold_prefill", "target_tokens": size, "rep": rep, "prompt_tokens": pt,
                               "ttft_s": r["ttft_s"], "prefill_tps": round(pt / r["ttft_s"], 1) if r["ttft_s"] else None,
                               "decode_tps": r["decode_tps"], "window": smp.window(t0, t1)})
        links = {f"gpu{i}": {"gen": g, "width": w} for i, (g, w) in sorted(smp.links.items())}
        save(a.label, {"test": "link_under_load", "links": links})
    finally:
        smp.stop()
    print(f"\nDone. flash is loaded. Compare later with: {sys.argv[0]} compare --a x8 --b x16")


def load(label):
    p = f"{OUT}/pcie-{label}.jsonl"
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else sys.exit(f"missing {p}")


def cmd_compare(a):
    A, B = load(a.a), load(a.b)

    def last(rs, test, **kw):
        c = [r for r in rs if r["test"] == test and all(r.get(k) == v for k, v in kw.items())]
        return c

    print(f"{'test':<26}{a.a:>12}{a.b:>12}{'change':>10}")
    la, lb = last(A, "load"), last(B, "load")
    if la and lb:
        x, y = la[-1]["load_s"], lb[-1]["load_s"]
        print(f"{'cold load (s)':<26}{x:>12}{y:>12}{(x - y) / x:>9.0%} faster")
    da, db = last(A, "decode"), last(B, "decode")
    if da and db:
        x, y = da[-1]["decode_tps_median"], db[-1]["decode_tps_median"]
        print(f"{'decode tok/s (control)':<26}{x:>12}{y:>12}{(y - x) / x:>+9.0%}")
    sizes = sorted({r["target_tokens"] for r in A + B if r["test"] == "cold_prefill"})
    for s in sizes:
        ta = [r["ttft_s"] for r in last(A, "cold_prefill", target_tokens=s)]
        tb = [r["ttft_s"] for r in last(B, "cold_prefill", target_tokens=s)]
        if ta and tb:
            x, y = statistics.median(ta), statistics.median(tb)
            print(f"{f'TTFT {s // 1024}k (s)':<26}{x:>12.1f}{y:>12.1f}{(x - y) / x:>9.0%} faster")
        ra = [max(r["window"][g]["rx_peak_mbs"] for g in r["window"]) for r in last(A, "cold_prefill", target_tokens=s)]
        rb = [max(r["window"][g]["rx_peak_mbs"] for g in r["window"]) for r in last(B, "cold_prefill", target_tokens=s)]
        if ra and rb:
            print(f"{f'  peak rx {s // 1024}k (MB/s)':<26}{max(ra):>12.0f}{max(rb):>12.0f}")
    for lab, rs in ((a.a, A), (a.b, B)):
        lk = last(rs, "link_under_load")
        if lk:
            print(f"link under load {lab}: {lk[-1]['links']}")


def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--label", required=True, help="e.g. x8 (before) or x16 (after)")
    r.add_argument("--sizes", default="32768,131072,262144")
    r.add_argument("--repeats", type=int, default=2)
    r.add_argument("--force", action="store_true", help="run even if flash is busy")
    c = sp.add_parser("compare")
    c.add_argument("--a", required=True)
    c.add_argument("--b", required=True)
    a = p.parse_args()
    {"run": cmd_run, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()

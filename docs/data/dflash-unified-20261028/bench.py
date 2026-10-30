#!/usr/bin/env python3
"""Standalone llama-server A/B harness (DFlash2 vs MTP, unified KV vs fixed slots).

Starts one llama-server on one GPU with the given flags, runs a workload set,
writes JSON lines to var/dflash-eval/results.jsonl. llama-swap is kept unloaded
for the duration (unload loop). Restore with: scripts/llama-swap-mode.py set daily
"""
import argparse, json, os, shlex, subprocess, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BIN = os.environ.get("LLAMA_SERVER_BIN", "/srv/ai/src/llama.cpp/build/bin/llama-server")
M = "/srv/ai/models"
OUT = "/srv/ai/var/dflash-eval"
PORT = 10150
SWAP = "http://127.0.0.1:9090"
Q6 = f"{M}/qwen3.8-27b/Qwen3.8-27B-Q6_K.gguf"
Q4 = f"{M}/qwen3.8-27b-q4/Qwen3.8-27B-UD-Q4_K_XL.gguf"
MMPROJ = f"{M}/qwen3.8-27b/mmproj-F16.gguf"
DFL = f"{M}/qwen3.8-27b-dflash2/Qwen3.8-27B-DFlash2-Q4_K_M.gguf"
BASE = "--cache-type-k q8_0 --cache-type-v q8_0 --batch-size 2048 --ubatch-size 2048 --flash-attn on --cont-batching --jinja --metrics --n-gpu-layers 999"
VIS = f"--mmproj {MMPROJ} --image-max-tokens 1024"

CONFIGS = {
    "q6-mtp3": (1, f"--model {Q6} {VIS} --ctx-size 163840 --parallel 1 --spec-type draft-mtp --spec-draft-n-max 3"),
    "q6-mtp4": (1, f"--model {Q6} {VIS} --ctx-size 163840 --parallel 1 --spec-type draft-mtp --spec-draft-n-max 4"),
    "q6-dfl7": (1, f"--model {Q6} {VIS} --ctx-size 131072 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 7"),
    "q6-dfl5": (1, f"--model {Q6} {VIS} --ctx-size 131072 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 5"),
    "q4-mtp3": (1, f"--model {Q4} {VIS} --ctx-size 163840 --parallel 1 --spec-type draft-mtp --spec-draft-n-max 3"),
    "q4-dfl7": (1, f"--model {Q4} {VIS} --ctx-size 163840 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 7"),
    "q6-dfl7-160k": (1, f"--model {Q6} {VIS} --ctx-size 163840 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 7"),
    "q6-dfl7-144k": (1, f"--model {Q6} {VIS} --ctx-size 147456 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 7"),
    "q6-dfl7-128k-d8": (1, f"--model {Q6} {VIS} --ctx-size 131072 --parallel 1 --spec-type draft-dflash -md {M}/qwen3.8-27b-dflash2/Qwen3.8-27B-DFlash2-Q8_0.gguf --spec-draft-n-ubatch 512 --spec-draft-n-max 7"),
    "q4-dfl5": (1, f"--model {Q4} {VIS} --ctx-size 163840 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 5"),
    "q4-dfl10": (1, f"--model {Q4} {VIS} --ctx-size 163840 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 10"),
    "q4-dfl7-262k": (1, f"--model {Q4} {VIS} --ctx-size 262144 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 7"),
    "q4-dfl7-262k-nov": (1, f"--model {Q4} --ctx-size 262144 --parallel 1 --spec-type draft-dflash -md {DFL} --spec-draft-n-ubatch 512 --spec-draft-n-max 7"),
    "p2-fixed": (1, f"--model {Q6} --ctx-size 204800 --parallel 2"),
    "p2-unified": (1, f"--model {Q6} --ctx-size 204800 --parallel 2 --kv-unified"),
}

FILLER = ("The quick brown fox jumps over the lazy dog near the riverbank while "
          "the sun sets slowly behind the distant mountains and birds fly home. ")
CODE_FN = ('def process_item_{i}(record, config, logger):\n    """Validate and transform a single record."""\n'
           '    if record is None:\n        logger.warning("null record at index %d", {i})\n        return None\n'
           '    value = record.get("value", 0) * config.scale + config.offset\n'
           '    if value > config.threshold:\n        logger.info("item {i} exceeds threshold: %s", value)\n'
           '    return {{"id": record["id"], "value": value, "ok": True}}\n\n')


def chat(user):
    return f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def p_echo(n):
    body = "".join(CODE_FN.format(i=i) for i in range(max(1, n // 90)))
    return chat("Return the COMPLETE python module verbatim, inserting the line `    # validated` as the first "
                "line inside each function body. Output only code.\n\n```python\n" + body + "```")


def p_gen():
    return chat("Write a Python asyncio web crawler class with rate limiting, retries with exponential backoff, "
                "robots.txt support and a pluggable storage backend. Include docstrings and a usage example.")


def p_filler(n, tag=""):
    return chat(f"{tag}Summarize the following text in two sentences.\n\n" + FILLER * max(1, n // 27))


def post(path, body, timeout=3600):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def comp(prompt, n=256, temp=0.0, cache=False):
    t0 = time.time()
    try:
        d = post("/completion", {"prompt": prompt, "n_predict": n, "temperature": temp, "cache_prompt": cache, "seed": 7})
    except Exception as e:
        return {"error": str(e)[:200], "wall": time.time() - t0}
    t = d.get("timings", {})
    return {"pp_n": t.get("prompt_n"), "pp_s": round(t.get("prompt_per_second", 0), 1),
            "tg_n": t.get("predicted_n"), "tg_s": round(t.get("predicted_per_second", 0), 1),
            "draft_n": t.get("draft_n"), "draft_ok": t.get("draft_n_accepted"),
            "wall": round(time.time() - t0, 1), "stop": d.get("stop_type")}


def vram(gpu):
    try:
        return int(subprocess.check_output(["nvidia-smi", "-i", str(gpu), "--query-gpu=memory.used",
                                            "--format=csv,noheader,nounits"], text=True).strip())
    except Exception:
        return 0


class Run:
    def __init__(self, name, gpu, args):
        self.name, self.gpu, self.args = name, gpu, args
        self.peak, self.stop = 0, False

    def __enter__(self):
        self.unl = threading.Thread(target=self._unload, daemon=True); self.unl.start()
        time.sleep(6)
        env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=str(self.gpu))
        self.log = open(f"{OUT}/{self.name}.server.log", "w")
        self.p = subprocess.Popen([BIN, "--host", "127.0.0.1", "--port", str(PORT)] + shlex.split(BASE) + shlex.split(self.args),
                                  env=env, stdout=self.log, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < 900:
            if self.p.poll() is not None:
                raise SystemExit(f"{self.name}: server exited rc={self.p.returncode}; see {OUT}/{self.name}.server.log")
            try:
                if json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3).read()).get("status") == "ok":
                    break
            except Exception:
                pass
            time.sleep(3)
        self.load_s = round(time.time() - t0)
        self.idle = vram(self.gpu)
        self.mon = threading.Thread(target=self._mon, daemon=True); self.mon.start()
        return self

    def _unload(self):
        while not self.stop:
            try:
                urllib.request.urlopen(SWAP + "/unload", timeout=10).read()
            except Exception:
                pass
            time.sleep(4)

    def _mon(self):
        while not self.stop:
            self.peak = max(self.peak, vram(self.gpu)); time.sleep(1)

    def __exit__(self, *a):
        self.p.terminate()
        try:
            self.p.wait(30)
        except Exception:
            self.p.kill()
        self.stop = True


def record(name, kind, data, run):
    row = {"cfg": name, "test": kind, "idle_mib": run.idle, "peak_mib": run.peak, "load_s": run.load_s, **data}
    with open(f"{OUT}/results.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def dec_suite(name, run, reps=3):
    comp(p_gen(), 32)  # warm
    for kind, mk, temp in (("echo4k", lambda: p_echo(4000), 0.0), ("gen", p_gen, 0.6), ("gen-t1", p_gen, 1.0)):
        for r in range(reps):
            record(name, kind, comp(mk(), 256, temp), run)
    for r in range(2):
        record(name, "pp16k", comp(p_filler(16000, f"[{r}] "), 16), run)


def fit_suite(name, run):
    record(name, "fill-120k", comp(p_filler(120000, "[fit] "), 64), run)


def conc_suite(name, run):
    for conc in (1, 2):
        with ThreadPoolExecutor(conc) as ex:
            t0 = time.time()
            rs = list(ex.map(lambda i: comp(p_gen().replace("crawler", f"crawler{i}"), 256, 0.6), range(conc)))
        tot = sum(r.get("tg_n") or 0 for r in rs)
        record(name, f"conc{conc}", {"agg_tg_s": round(tot / (time.time() - t0), 1),
                                      "per_req": [r.get("tg_s") for r in rs]}, run)
    # one prompt larger than a fixed slot (100k) must still fit if the pool is unified
    record(name, "big-130k", comp(p_filler(130000, "[big] "), 32), run)
    # decode while a second slot prefills a long prompt
    res = {}
    def a():
        res["dec"] = comp(p_gen(), 512, 0.6)
    def b():
        time.sleep(4); res["pre"] = comp(p_filler(30000, "[side] "), 16)
    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    [t.start() for t in ts]; [t.join() for t in ts]
    record(name, "decode-during-prefill", {"dec_tg_s": res["dec"].get("tg_s"), "dec_wall": res["dec"].get("wall"),
                                          "pre_wall": res["pre"].get("wall"), "pre_err": res["pre"].get("error")}, run)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cfgs", nargs="+")
    ap.add_argument("--suite", default="dec", choices=["dec", "fit", "conc", "dec+fit"])
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    for c in a.cfgs:
        gpu, args = CONFIGS[c]
        with Run(c, gpu, args) as run:
            if "dec" in a.suite:
                dec_suite(c, run, a.reps)
            if "fit" in a.suite:
                fit_suite(c, run)
            if a.suite == "conc":
                conc_suite(c, run)
            record(c, "final", {}, run)

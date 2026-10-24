#!/usr/bin/env python3
"""Window 6: Strata vision, then big head-to-head. Log w6/window6.log; markers w6/done.<stage>."""
import json, os, signal, subprocess, sys, time, urllib.request, traceback
S = "/srv/ai/models/qwen3.8-flash-next/strata"; E = "/srv/ai/var/flash-eval"; W = f"{E}/w6"; AI = "/srv/ai"
os.makedirs(W, exist_ok=True)
LOG = open(f"{W}/window6.log", "a", buffering=1)
def log(*a): print(time.strftime("%H:%M:%SZ", time.gmtime()), *a, file=LOG, flush=True)
def done(st): return os.path.exists(f"{W}/done.{st}")
def mark(st): open(f"{W}/done.{st}", "w").write(time.strftime("%FT%TZ", time.gmtime()))
def sh(cmd, out, cwd=AI, env=None, tmo=None):
    log("RUN", cmd[:220]); e = dict(os.environ); e.update(env or {})
    try:
        with open(out, "a") as f: r = subprocess.run(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=e, timeout=tmo)
        log("  rc", r.returncode); return r.returncode
    except subprocess.TimeoutExpired: log("  TIMEOUT"); return -1
def stop(p):
    try: os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    for _ in range(40):
        time.sleep(3)
        if p.poll() is not None: break
    time.sleep(10)
def wait(url, p, n=240):
    for _ in range(n):
        time.sleep(5)
        if p.poll() is not None: return False
        try: urllib.request.urlopen(url, timeout=3); return True
        except Exception: pass
    return False

def strata_vision():
    if done("strata-vision"): return
    log("== stage strata-vision")
    p = subprocess.Popen([f"{E}/strata-serve.sh", f"{S}/strata-w6.json"], stdout=open(f"{W}/strata.srv", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    try:
        if not wait("http://127.0.0.1:8080/v1/models", p): log("  strata failed to start"); return
        sh(f"python3 {E}/vision2.py http://127.0.0.1:8080/v1/chat/completions flash {W}/vision-flash.json", f"{W}/vision-flash.out", cwd=E)
        sh(f"python3 {E}/flash-bench.py speed --url http://127.0.0.1:8080 --label q4w6-vision --tokens 384 --repeats 3", f"{W}/speed-vision.out", cwd=E)
        mark("strata-vision")
    finally: stop(p)

BIG = ("/srv/ai/src/llama.cpp/build/bin/llama-server --model /srv/ai/models/qwen3.8-27b/Qwen3.8-27B-UD-Q8_K_XL.gguf "
       "--mmproj /srv/ai/models/qwen3.8-27b/mmproj-F16.gguf --image-max-tokens 1024 --ctx-size 262144 --cache-type-k f16 --cache-type-v f16 "
       "--split-mode layer --parallel 1 --spec-type draft-mtp --spec-draft-n-max 2 --chat-template-kwargs {\"reasoning_effort\":\"medium\"} "
       "--host 127.0.0.1 --port 8082 --n-gpu-layers 999 --flash-attn on --cont-batching --jinja --metrics")
URL = "http://127.0.0.1:8082"; TESTS = [("dungeon-adventure-engine", "py", ""), ("local-dungeon-web", "html", ""), ("localmind-landing-page", "html", ""),
         ("localmind-landing-page-pro", "html", ""), ("logic-puzzle-reasoning", "md", "--no-extract")]
def big_stage():
    log("== stage big")
    import shlex
    cmd = shlex.split(BIG.replace('{"reasoning_effort":"medium"}', "'{\"reasoning_effort\":\"medium\"}'"))
    p = subprocess.Popen(cmd, env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "1,2"},
                         stdout=open(f"{W}/big.srv", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    try:
        if not wait(URL + "/health", p, 360): log("  big failed to start"); return
        steps = [
         ("vision", lambda: sh(f"python3 {E}/vision2.py {URL}/v1/chat/completions x {W}/vision-big.json", f"{W}/vision-big.out", cwd=E)),
         ("tools", lambda: sh(f"python3 {E}/toolcall-big.py --models x --repeat 2 --out {W}/toolcalls-big.json", f"{W}/toolcalls-big.out", cwd=E)),
         ("speed", lambda: (sh(f"python3 {E}/flash-bench.py speed --url {URL} --label big-h2h --tokens 384 --repeats 3", f"{W}/big-speed.out", cwd=E),
                            sh(f"python3 {E}/flash-bench.py prompt --url {URL} --label big-h2h --sizes 8192,32768,131072", f"{W}/big-prompt.out", cwd=E))),
         ("needle", lambda: sh(f"python3 {E}/flash-bench.py needle --url {URL} --label big-h2h --filler real --sizes 100000,160000 --depths 0.1,0.5,0.9", f"{W}/big-needle.out", cwd=E)),
         ("copilot", copilot),
         ("gsm8k", lambda: sh(f"scripts/lm-eval-run.sh big-h2h {URL}/v1/chat/completions gsm8k 100 6144 1 'until=<|im_end|>'", f"{W}/big-gsm8k.out", tmo=5*3600)),
         ("code", code)]
        for name, fn in steps:
            if done(f"big-{name}"): continue
            log("  step", name)
            try: fn(); mark(f"big-{name}")
            except Exception: log("  ERROR", name, traceback.format_exc())
            if p.poll() is not None: log("  big server died"); break
    finally: stop(p)

def copilot():
    for t, ext, _ in [("local-dungeon-web", "html", ""), ("dungeon-adventure-engine", "py", "")]:
        sh(f"python3 scripts/eval-run-copilot.py --test {t} --model big --label big-h2h-copilot --base-url {URL}/v1 --ext {ext} --max-prompt-tokens 163840 --max-output-tokens 32000 --timeout 3000",
           f"{W}/copilot-{t}.out", env={"COPILOT_PROVIDER_API_KEY": "none"}, tmo=3600)
def code():
    for t, ext, extra in TESTS:
        procs = [subprocess.Popen(f"python3 scripts/eval-run.py --test {t} --model big --label big-h2h-r{i} --endpoint {URL}/v1/chat/completions --temp 0.7 --ext {ext} {extra} --max-tokens 32000 --timeout 7200 --cmd 'llama.cpp b11819 Qwen3.8-27B UD-Q8_K_XL MTP2 reasoning medium (big slot)'",
                                  shell=True, cwd=AI, stdout=open(f"{W}/code-{t}-r{i}.out", "w"), stderr=subprocess.STDOUT) for i in (1, 2, 3)]
        for p in procs: p.wait()
        log("  eval done", t, [p.returncode for p in procs])
    for t, ext, extra in TESTS:
        sh(f"python3 evals/{t}/check.py", f"{W}/check-{t}.out"); sh(f"python3 scripts/eval-summary.py --test {t}", f"{W}/summary-{t}.out")

if __name__ == "__main__":
    log("window6 start")
    try:
        strata_vision(); big_stage()
    except Exception: log("FATAL", traceback.format_exc())
    log("WINDOW6_DONE")

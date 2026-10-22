#!/usr/bin/env python3
"""E6: multi-turn prompt-cache / TTFT probe against one llama-server build.

Starts the production `coding` command line on one GPU (A = b9850, B = b11819), sends a ~20k-token
document plus follow-up turns through /v1/chat/completions, and prints per turn how many prompt
tokens were reused (cache_n) versus re-processed (prompt_n).
"""
import argparse, json, os, re, signal, socket, subprocess, sys, time, urllib.request

BINS = {"A": "/srv/ai/src/llama.cpp/build/bin/llama-server",
        "B": "/srv/ai/src/llama.cpp-b11819/build/bin/llama-server"}
MODEL = "/srv/ai/models/qwen3.8-27b/Qwen3.8-27B-Q6_K.gguf"
QUESTIONS = [
    "Based on the runbook above, what are the steps to rotate the LiteLLM master key? Be brief.",
    "Which GPU index is the Titan X and what power cap does it have? One or two sentences.",
    "What does the runbook say about mcpo hot-reload? One or two sentences.",
    "How must the SearXNG URL be configured for Open WebUI? One or two sentences.",
    "What are the quiet hours and which timezone do they use? One or two sentences.",
    "Which services does the status dashboard need a sign-in for? One or two sentences.",
]


def free_port():
    for p in range(10140, 10180):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p)); s.close(); return p
        except OSError:
            s.close()
    return 10179


def post(port, path, body, timeout=900):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", choices=list(BINS), required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--gpu", default="1")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--ctx", type=int, default=163840)
    ap.add_argument("--doc", default="/srv/ai/docs/server-setup.md")
    ap.add_argument("--doc-tokens", type=int, default=20000)
    ap.add_argument("--turns", type=int, default=5)
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--think", choices=["on", "off"], default="on")
    ap.add_argument("--extra", default="", help="extra llama-server flags (space separated)")
    ap.add_argument("--out", default="/srv/ai/tmp/lcpp-test/ab1/e6")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    port = free_port()
    cmd = [BINS[a.build], "--model", a.model, "--host", "127.0.0.1", "--port", str(port),
           "--n-gpu-layers", "999", "--flash-attn", "on", "--cont-batching", "--jinja", "--metrics",
           "--ctx-size", str(a.ctx), "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--parallel", "1",
           "--batch-size", "2048", "--ubatch-size", "2048", "--spec-type", "draft-mtp",
           "--spec-draft-n-max", "3", "--chat-template-kwargs", '{"reasoning_effort":"medium"}'] + a.extra.split()
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": a.gpu}
    logf = f"{a.out}/{a.label}.server.log"
    lf = open(logf, "w"); lf.write("CMD: " + " ".join(cmd) + "\n"); lf.flush()
    proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env, preexec_fn=os.setsid)
    try:
        for _ in range(180):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(2)
        else:
            print("server never became ready"); return 1

        text = open(a.doc, errors="ignore").read()
        n = int(a.doc_tokens * 3.7)
        for _ in range(4):  # trim the document to ~doc-tokens using the server's own tokenizer
            toks = len(post(port, "/tokenize", {"content": text[:n]})["tokens"])
            if abs(toks - a.doc_tokens) < 300:
                break
            n = int(n * a.doc_tokens / toks)
        doc = text[:n]
        print(f"[{a.label}] build {a.build}, doc {toks} tokens, think {a.think}, extra '{a.extra}'", flush=True)

        msgs = [{"role": "system", "content": "You are a concise assistant."}]
        rows = []
        for i in range(a.turns + 1):
            q = (f"Here is a runbook:\n\n{doc}\n\n---\n{QUESTIONS[0]}" if i == 0 else QUESTIONS[i % len(QUESTIONS)])
            msgs.append({"role": "user", "content": q})
            body = {"model": "x", "messages": msgs, "max_tokens": a.max_tokens, "temperature": 0.0, "stream": False}
            if a.think == "off":
                body["chat_template_kwargs"] = {"enable_thinking": False, "reasoning_effort": "medium"}
            t0 = time.time()
            r = post(port, "/v1/chat/completions", body)
            wall = time.time() - t0
            tm = r.get("timings", {})
            m = r["choices"][0]["message"]
            content = m.get("content") or ""
            think_chars = len(m.get("reasoning_content") or "")
            msgs.append({"role": "assistant", "content": content})
            row = dict(turn=i + 1, cache_n=tm.get("cache_n"), prompt_n=tm.get("prompt_n"),
                       prefill_s=round((tm.get("prompt_ms") or 0) / 1000, 2),
                       gen_n=tm.get("predicted_n"), decode_tps=round(tm.get("predicted_per_second") or 0, 1),
                       wall_s=round(wall, 2), think_chars=think_chars, answer_chars=len(content),
                       finish=r["choices"][0].get("finish_reason"))
            rows.append(row)
            print("  " + " | ".join(f"{k} {v}" for k, v in row.items()), flush=True)
        json.dump(rows, open(f"{a.out}/{a.label}.json", "w"), indent=1)
        time.sleep(1)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
        for _ in range(60):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    log = open(logf, errors="ignore").read()
    for pat in ("forcing full prompt re-processing", "restored context checkpoint", "created context checkpoint",
                "erased invalidated context checkpoint", "erasing old context checkpoint", "deprecated", "warn"):
        print(f"  log '{pat}': {len(re.findall(pat, log, re.I))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

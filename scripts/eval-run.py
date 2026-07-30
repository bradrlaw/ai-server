#!/usr/bin/env python3
"""Run an eval prompt against a model and save its output + performance metrics.

Sends evals/<test>/prompt.txt to an OpenAI-compatible chat endpoint, extracts the
primary code block from the reply, and writes the result under
evals/<test>/outputs/<label>/:
  - index.html (or output.<ext>) — the extracted answer
  - raw.txt                      — full reply (reasoning_content + content)
  - meta.json                    — sampler, usage, finish_reason, load command,
                                   and server-side performance timings
  - run.html                     — a human-readable view of this run (metrics +
                                   the rendered output inline + link to raw)
After each run it also refreshes evals/<test>/summary.html (the comparison page).

Performance metrics come from llama.cpp's `timings` block in the response
(server-side, network-independent):
  - ttft_ms      = prompt_ms          (prefill / time to first token)
  - prefill_tps  = prompt_per_second
  - decode_tps   = predicted_per_second
The exact llama.cpp launch command, model file, and MTP state are read from the
llama-swap router (/running + the model's /props). For a standalone --endpoint,
pass --cmd to record the launch command manually; /props still supplies the path.

Reasoning models emit their thinking in `reasoning_content`; only `content` is
used for the answer, but the full reply is preserved in raw.txt.

Examples:
  scripts/eval-run.py --test localmind-landing-page --model coding
  scripts/eval-run.py --test localmind-landing-page --model gemma-31b --temp 0.7
  scripts/eval-run.py --test localmind-landing-page --model thinkingcap \
      --endpoint http://127.0.0.1:8902/v1/chat/completions --label thinkingcap-27b

Default endpoint is the llama-swap router (http://127.0.0.1:9090/v1/chat/completions),
which loads the requested `--model` on demand.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load the summary builder (hyphenated filename -> load by path).
_spec = importlib.util.spec_from_file_location(
    "eval_summary", os.path.join(REPO, "scripts", "eval-summary.py"))
eval_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_summary)


def post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_json(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


FENCE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)


def extract_code(content, want_ext):
    """Return (code, extracted_bool, kind). Prefer a fenced block; fall back to raw
    HTML sniffing; else return the whole content unextracted."""
    blocks = FENCE.findall(content)
    if blocks:
        lang_pref = {"html": {"html", "htm"}, "js": {"js", "javascript"},
                     "py": {"python", "py"}}.get(want_ext, {want_ext})
        tagged = [b for b in blocks if (b[0] or "").lower() in lang_pref]
        chosen = tagged[0] if tagged else max(blocks, key=lambda b: len(b[1]))
        return chosen[1].strip() + "\n", True, "fenced"
    if want_ext in ("html", "htm"):
        m = re.search(r"(?is)(<!doctype html.*|<html[ >].*)", content)
        if m:
            return m.group(1).strip() + "\n", True, "sniffed-html"
    return content, False, "raw"


def resolve_load_info(endpoint, model, manual_cmd):
    """Best-effort: return (load_command, proxy_url, model_path).

    For the llama-swap router, read the fully-expanded launch command and upstream
    proxy from /running, then the served model_path from that upstream's /props.
    For a standalone server, use --cmd (if given) and the endpoint's own /props.
    """
    u = urlparse(endpoint)
    base = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
    load_cmd, proxy, model_path = manual_cmd, None, None

    running = get_json(base + "/running")
    if running and isinstance(running.get("running"), list):
        for e in running["running"]:
            if e.get("model") == model:
                load_cmd = load_cmd or (e.get("cmd") or "").strip()
                proxy = e.get("proxy")
                break

    props = get_json((proxy or base) + "/props") or get_json(base + "/props")
    if props:
        model_path = props.get("model_path")
    return load_cmd, proxy, model_path


def perf_from_timings(t, wall_secs):
    t = t or {}
    dn, da = t.get("draft_n"), t.get("draft_n_accepted")
    accept = round(da / dn, 3) if dn else None
    perf = {
        "wall_secs": wall_secs,
        "ttft_ms": t.get("prompt_ms"),
        "prefill_tps": t.get("prompt_per_second"),
        "decode_tps": t.get("predicted_per_second"),
        "prompt_tokens": t.get("prompt_n"),
        "predicted_tokens": t.get("predicted_n"),
        "prefill_ms": t.get("prompt_ms"),
        "decode_ms": t.get("predicted_ms"),
    }
    return perf, {"draft_n": dn, "draft_n_accepted": da, "accept_rate": accept}




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", required=True, help="test dir name under evals/")
    ap.add_argument("--model", required=True, help="model id to request")
    ap.add_argument("--label", help="output subdir name (default: --model)")
    ap.add_argument("--endpoint",
                    default="http://127.0.0.1:9090/v1/chat/completions")
    ap.add_argument("--ext", default="html", help="expected output file extension")
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--top-p", type=float, default=0.95)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--system", default=None, help="optional system prompt")
    ap.add_argument("--cmd", default=None,
                    help="record this llama.cpp launch command (standalone servers)")
    ap.add_argument("--no-extract", action="store_true",
                    help="save the full reply verbatim as the output (skip code-block "
                         "extraction) — use for prose/reasoning tests")
    args = ap.parse_args()

    test_dir = os.path.join(REPO, "evals", args.test)
    prompt_path = os.path.join(test_dir, "prompt.txt")
    if not os.path.isfile(prompt_path):
        sys.exit(f"no prompt at {prompt_path}")
    prompt = open(prompt_path).read()

    label = args.label or args.model
    out_dir = os.path.join(test_dir, "outputs", label)
    os.makedirs(out_dir, exist_ok=True)

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": args.model, "messages": messages,
               "temperature": args.temp, "top_p": args.top_p, "top_k": args.top_k,
               "max_tokens": args.max_tokens,
               # Force a full cold prefill so TTFT / prefill-tps are real and
               # comparable across runs (don't reuse a warm KV cache from a prior run).
               "cache_prompt": False}

    print(f"→ {args.model} @ {args.endpoint} (temp={args.temp}, max_tokens={args.max_tokens})",
          flush=True)
    t0 = time.time()
    try:
        d = post(args.endpoint, payload, args.timeout)
    except Exception as e:
        sys.exit(f"request failed: {e}")
    dt = round(time.time() - t0, 1)

    msg = d["choices"][0]["message"]
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    usage = d.get("usage", {})
    finish = d["choices"][0].get("finish_reason")
    timings = d.get("timings") or {}

    code, extracted, kind = extract_code(content, args.ext)
    if args.no_extract:
        code, extracted, kind = content, False, "full-content"
    is_html = args.ext in ("html", "htm")
    out_name = f"index.{args.ext}" if is_html else f"output.{args.ext}"
    with open(os.path.join(out_dir, out_name), "w") as f:
        f.write(code)
    with open(os.path.join(out_dir, "raw.txt"), "w") as f:
        if reasoning:
            f.write("===== reasoning_content =====\n" + reasoning +
                    "\n\n===== content =====\n")
        f.write(content)

    load_cmd, proxy, model_path = resolve_load_info(args.endpoint, args.model, args.cmd)
    perf, mtp = perf_from_timings(timings, dt)
    mtp["enabled"] = bool(
        (load_cmd and "draft-mtp" in load_cmd) or (mtp.get("draft_n") or 0) > 0)
    model_name = os.path.basename(model_path) if model_path else None

    meta = {
        "test": args.test, "model_slot": args.model, "label": label,
        "model_name": model_name, "model_path": model_path,
        "endpoint": args.endpoint, "proxy": proxy,
        "sampler": {"temperature": args.temp, "top_p": args.top_p,
                    "top_k": args.top_k, "max_tokens": args.max_tokens},
        "load_command": load_cmd,
        "usage": usage, "finish_reason": finish,
        "performance": perf, "mtp": mtp,
        "server_timings": timings,
        "extraction": {"extracted": extracted, "kind": kind},
        "output_file": out_name,
        "output_bytes": len(code.encode()),
        "reasoning_bytes": len(reasoning.encode()),
        "wall_secs": dt,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    # build_summary() (re)generates this run's run.html + the comparison page,
    # picking up check.json for any output that has already been scored.
    summary_path = eval_summary.build_summary(test_dir)

    warn = "" if extracted else "  ⚠ no code block found — saved raw content"
    trunc = "  ⚠ TRUNCATED (hit max_tokens)" if finish == "length" else ""
    print(f"✓ {label}: {meta['output_bytes']} B via {kind} in {dt}s "
          f"(completion {usage.get('completion_tokens')} tok, "
          f"ttft {perf.get('ttft_ms')}ms, decode {perf.get('decode_tps')} t/s, "
          f"mtp={'on' if mtp['enabled'] else 'off'}, finish={finish})"
          f"{warn}{trunc}", flush=True)
    print(f"  → {os.path.relpath(os.path.join(out_dir, out_name), REPO)}")
    print(f"  → {os.path.relpath(os.path.join(out_dir, 'run.html'), REPO)}")
    print(f"  → {os.path.relpath(summary_path, REPO)}")


if __name__ == "__main__":
    main()

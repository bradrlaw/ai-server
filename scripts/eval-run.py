#!/usr/bin/env python3
"""Run an eval prompt against a model and save its output.

Sends evals/<test>/prompt.txt to an OpenAI-compatible chat endpoint, extracts the
primary code block from the reply, and writes the result under
evals/<test>/outputs/<label>/ (index.html or output.txt + raw.txt + meta.json).

Reasoning models emit their thinking in `reasoning_content`; only `content` is used
for the answer, but the full reply (incl. reasoning) is preserved in raw.txt.

Examples:
  scripts/eval-run.py --test localmind-landing-page --model coding
  scripts/eval-run.py --test localmind-landing-page --model gemma-31b --temp 0.7
  scripts/eval-run.py --test localmind-landing-page --model thinkingcap \
      --endpoint http://127.0.0.1:8902/v1/chat/completions --label thinkingcap-27b

Default endpoint is the llama-swap router (http://127.0.0.1:9090/v1/chat/completions),
which loads the requested `--model` on demand.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


FENCE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)


def extract_code(content, want_ext):
    """Return (code, extracted_bool, kind). Prefer a fenced block; fall back to raw
    HTML sniffing; else return the whole content unextracted."""
    blocks = FENCE.findall(content)
    if blocks:
        # prefer a block whose language tag matches the wanted extension family
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
               "max_tokens": args.max_tokens}

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

    code, extracted, kind = extract_code(content, args.ext)
    out_name = f"index.{args.ext}" if args.ext in ("html", "htm") else f"output.{args.ext}"
    with open(os.path.join(out_dir, out_name), "w") as f:
        f.write(code)
    with open(os.path.join(out_dir, "raw.txt"), "w") as f:
        if reasoning:
            f.write("===== reasoning_content =====\n" + reasoning +
                    "\n\n===== content =====\n")
        f.write(content)

    meta = {
        "test": args.test, "model": args.model, "label": label,
        "endpoint": args.endpoint,
        "sampler": {"temperature": args.temp, "top_p": args.top_p,
                    "top_k": args.top_k, "max_tokens": args.max_tokens},
        "usage": usage, "finish_reason": finish,
        "extraction": {"extracted": extracted, "kind": kind},
        "output_file": out_name,
        "output_bytes": len(code.encode()),
        "reasoning_bytes": len(reasoning.encode()),
        "wall_secs": dt,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    warn = "" if extracted else "  ⚠ no code block found — saved raw content"
    trunc = "  ⚠ TRUNCATED (hit max_tokens)" if finish == "length" else ""
    print(f"✓ {label}: {meta['output_bytes']} B via {kind} in {dt}s "
          f"(completion {usage.get('completion_tokens')} tok, finish={finish})"
          f"{warn}{trunc}", flush=True)
    print(f"  → {os.path.relpath(os.path.join(out_dir, out_name), REPO)}")


if __name__ == "__main__":
    main()

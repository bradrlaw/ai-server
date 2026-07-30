#!/usr/bin/env python3
"""Agent-mediated eval runner: run any evals/<test> through GitHub Copilot CLI
(BYOK) against the LOCAL models, instead of hitting the chat endpoint directly.

Where eval-run.py measures the RAW model (one verbatim user message, one shot,
extract a fenced block), this measures the model + the Copilot scaffold: Copilot's
system prompt, its multi-turn tool loop (the agent writes the deliverable to a
file with its edit/create tools and can self-correct), and BYOK token budgets.
Same weights, so an A/B against the raw run isolates the harness effect.

It reuses the SAME test prompt, the SAME check.py, and the SAME summary/scoreboard
as eval-run.py — the run just lands under a distinct label (default
"<model>-copilot") so it sits beside the raw run in outputs/.

Usage:
  scripts/eval-run-copilot.py --test local-dungeon-web --model coding
  scripts/eval-run-copilot.py --test dungeon-adventure-engine --model chat --ext py

Requires: the `copilot` CLI on PATH and LITELLM_MASTER_KEY in docker/.env (the
BYOK key), i.e. the same setup as scripts/copilot-byok.sh.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load the summary builder (hyphenated filename -> load by path).
_spec = importlib.util.spec_from_file_location(
    "eval_summary", os.path.join(REPO, "scripts", "eval-summary.py"))
eval_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_summary)

FENCE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)

# Per-model prompt/output token budgets — mirrors scripts/copilot-byok.sh so the
# BYOK client stays within each model's shared llama.cpp KV ctx-size. Keep in sync
# with that script (and config/llama-swap.base.yaml) if ctx-sizes change.
TOKEN_BUDGETS = {
    "coding":     (131072, 32768),
    "chat":       (57344, 24576),
    "big":        (163840, 32768),
    "coder-next": (98304, 32768),
    "fast":       (24576, 8192),
    "fast-12b":   (98304, 8192),
}
DEFAULT_BUDGET = (32768, 8192)


def load_env_key(env_file):
    """Return LITELLM_MASTER_KEY from docker/.env without importing it wholesale."""
    if not os.path.isfile(env_file):
        return None
    for line in open(env_file, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line.startswith("LITELLM_MASTER_KEY="):
            v = line.split("=", 1)[1].strip()
            return v[1:-1] if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0] else v
    return None


def build_prompt(base_prompt, out_name):
    """The verbatim test prompt + a harness directive telling the agent to write
    the deliverable to a file (the raw prompt asks for an inline code block; in
    agent mode we want a file on disk instead)."""
    return (base_prompt.rstrip()
            + "\n\n"
            + "-" * 40 + "\n"
            + "BUILD INSTRUCTIONS (from the eval harness — not part of the "
              "deliverable itself):\n"
            + f"- Produce the deliverable described above as a single file named "
              f"`{out_name}` in your current working directory.\n"
            + "- Create the file using your file tools; do NOT paste the file "
              "contents into your chat reply.\n"
            + "- Do not create any other files or a project scaffold — just the "
              "one deliverable file.\n"
            + f"- When `{out_name}` is complete and satisfies the spec above, stop.\n")


def find_output(workdir, out_name, ext):
    """Locate the deliverable the agent wrote. Prefer the exact requested name;
    otherwise fall back to the largest file with the expected extension."""
    exact = os.path.join(workdir, out_name)
    if os.path.isfile(exact):
        return exact, "agent-file-write"
    candidates = []
    for root, _dirs, files in os.walk(workdir):
        for fn in files:
            if fn.lower().endswith("." + ext.lower()):
                p = os.path.join(root, fn)
                candidates.append((os.path.getsize(p), p))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1], "agent-file-fallback"
    return None, "none"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", required=True, help="evals/<test> directory name")
    ap.add_argument("--model", default="coding",
                    help="BYOK model id / COPILOT_MODEL (coding|chat|big|coder-next|fast)")
    ap.add_argument("--label", default=None, help="output label (default <model>-copilot)")
    ap.add_argument("--ext", default="html", help="expected output file extension")
    ap.add_argument("--base-url", default="http://127.0.0.1:4000/v1",
                    help="OpenAI-compatible BYOK endpoint (LiteLLM gateway)")
    ap.add_argument("--env-file", default=os.path.join(REPO, "docker", ".env"),
                    help="file holding LITELLM_MASTER_KEY")
    ap.add_argument("--max-prompt-tokens", type=int, default=None)
    ap.add_argument("--max-output-tokens", type=int, default=None)
    ap.add_argument("--reasoning-effort", default=None,
                    choices=[None, "none", "minimal", "low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--timeout", type=int, default=1800, help="hard wall-clock cap (s)")
    ap.add_argument("--keep-workdir", action="store_true",
                    help="don't delete the temp working dir (debugging)")
    args = ap.parse_args()

    test_dir = os.path.join(REPO, "evals", args.test)
    prompt_path = os.path.join(test_dir, "prompt.txt")
    if not os.path.isfile(prompt_path):
        sys.exit(f"no prompt at {prompt_path}")
    base_prompt = open(prompt_path, encoding="utf-8").read()

    if not shutil.which("copilot"):
        sys.exit("`copilot` CLI not found on PATH")

    api_key = (os.environ.get("COPILOT_PROVIDER_API_KEY")
               or os.environ.get("LITELLM_MASTER_KEY")
               or load_env_key(args.env_file))
    if not api_key:
        sys.exit(f"LITELLM_MASTER_KEY not set and not found in {args.env_file}")

    label = args.label or f"{args.model}-copilot"
    is_html = args.ext in ("html", "htm")
    out_name = f"index.{args.ext}" if is_html else f"output.{args.ext}"
    out_dir = os.path.join(test_dir, "outputs", label)
    os.makedirs(out_dir, exist_ok=True)

    def_prompt, def_output = TOKEN_BUDGETS.get(args.model, DEFAULT_BUDGET)
    max_prompt = args.max_prompt_tokens or def_prompt
    max_output = args.max_output_tokens or def_output

    env = dict(os.environ)
    env.update({
        "COPILOT_PROVIDER_BASE_URL": args.base_url,
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_API_KEY": api_key,
        "COPILOT_MODEL": args.model,
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": str(max_prompt),
        "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS": str(max_output),
        "COPILOT_PLAN_BUILD_MCP": "0",       # keep the model pinned; no planner->coder swap
        "COPILOT_AUTO_UPDATE": "false",
    })

    workdir = tempfile.mkdtemp(prefix=f"eval-copilot-{args.test}-{args.model}-")
    wrapped = build_prompt(base_prompt, out_name)

    cmd = [
        "copilot", "-p", wrapped, "-C", workdir,
        "--allow-all-tools",              # required for non-interactive
        "--no-ask-user",                  # autonomous
        "--no-color", "--silent", "--log-level", "error",
        "--no-custom-instructions",       # ignore repo AGENTS.md — fair, isolated
        "--disable-builtin-mcps",         # no github-mcp-server
        "--disable-mcp-server", "plan-build",  # no model-swapping planner pipeline
        "--no-remote", "--no-remote-export", "--no-auto-update",
    ]
    if args.reasoning_effort:
        cmd += ["--effort", args.reasoning_effort]

    print(f"→ {args.model} via Copilot CLI @ {args.base_url} "
          f"(prompt<={max_prompt}, output<={max_output}, workdir={workdir})",
          flush=True)
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, cwd=workdir, capture_output=True,
                              text=True, timeout=args.timeout)
        transcript, timed_out = proc.stdout + proc.stderr, False
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        transcript = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) \
            else "(timed out)"
        timed_out, rc = True, None
    dt = round(time.time() - t0, 1)

    src, kind = find_output(workdir, out_name, args.ext)
    if src:
        code = open(src, encoding="utf-8", errors="replace").read()
        finish = "stop"
    else:
        # Fall back to a fenced block in the transcript, if the agent printed one.
        m = None
        for cand in FENCE.finditer(transcript):
            m = cand
        code = m.group(2) if m else ""
        kind = "transcript-fence" if m else "none"
        finish = "no-file" if not m else "transcript-fence"

    with open(os.path.join(out_dir, out_name), "w") as f:
        f.write(code)
    with open(os.path.join(out_dir, "raw.txt"), "w") as f:
        f.write(transcript)

    ver = subprocess.run(["copilot", "--version"], capture_output=True, text=True)
    meta = {
        "test": args.test, "model_slot": args.model, "label": label,
        "model_name": f"{args.model} (Copilot CLI)", "model_path": None,
        "endpoint": args.base_url, "proxy": "litellm",
        "harness": "github-copilot-cli",
        "harness_version": (ver.stdout or ver.stderr).strip() or None,
        "sampler": {"note": "sampler controlled by Copilot CLI (not set by harness)",
                    "max_prompt_tokens": max_prompt, "max_output_tokens": max_output,
                    "reasoning_effort": args.reasoning_effort},
        "prompt_wrapped": True,
        "usage": {}, "finish_reason": finish,
        "performance": {}, "mtp": {"enabled": False},
        "extraction": {"extracted": bool(code), "kind": kind},
        "output_file": out_name,
        "output_bytes": len(code.encode()),
        "wall_secs": dt,
        "return_code": rc, "timed_out": timed_out,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    if not args.keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)

    summary_path = eval_summary.build_summary(test_dir)

    status = "✓" if code else "✗"
    warn = ""
    if timed_out:
        warn = "  ⚠ TIMED OUT"
    elif not code:
        warn = "  ⚠ no output file produced (see raw.txt transcript)"
    print(f"{status} {label}: {meta['output_bytes']} B via {kind} in {dt}s "
          f"(rc={rc}, finish={finish}){warn}", flush=True)
    print(f"  → {os.path.relpath(os.path.join(out_dir, out_name), REPO)}")
    print(f"  → {os.path.relpath(os.path.join(out_dir, 'run.html'), REPO)}")
    print(f"  → {os.path.relpath(summary_path, REPO)}")
    print(f"  Now score objective checks: evals/{args.test}/check.py")


if __name__ == "__main__":
    main()

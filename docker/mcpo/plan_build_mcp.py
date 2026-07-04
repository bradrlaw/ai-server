#!/usr/bin/env python3
"""plan-build MCP server for /srv/ai.

Exposes a planner -> coder pipeline as MCP tools, surfaced to Open WebUI through
mcpo (OpenAPI). The general chat model (`fast`, on the P100) calls these tools
and relays their output; the tools do the heavy lifting on the V100s:

  * `make_plan`       - a reasoning model designs a detailed plan (planning only)
  * `plan_and_build`  - plan with a reasoning model, then implement with a coder
  * `fast_plan_and_build` - quick plan+build on the resident V100 models (no swap)
  * `fast_make_plan`  - plan only with the resident `chat` model (no swap)
  * `implement_spec`  - implement directly from a given spec/plan (no planning)
  * `fast_implement_spec` - implement with the resident `coding` model (no swap)
  * `reset_models`    - "done": warm the default V100 models back (evict big/coder-next)

GPU model: the default planner is `big` (highest-precision, dual-V100) and the
default coder is `coder-next` (dual-V100). Both live on the two V100s and swap
each other in as needed; the `fast` chat model on the P100 is never evicted, so
the conversation that invokes these tools keeps responding. Callers can override
the planner (`fast`/`chat`/`big`) or coder per call.

Runtime: launched by mcpo via `uv run --with mcp` inside the mcpo container.
It talks to the LiteLLM gateway (host :4000, reached at host.docker.internal).
Secrets (LiteLLM master key) come from the container environment, never args.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://host.docker.internal:4000/v1")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
# Reasoning planners (esp. `big`) can spend minutes thinking; allow long calls.
HTTP_TIMEOUT = float(os.environ.get("PLAN_BUILD_TIMEOUT", "1800"))
# Only models that live *exclusively* on the P100 (never evicted) may call these
# tools, because the tools swap `big`/`coder-next` onto the V100s and would
# otherwise evict the calling conversation model. The caller reports its GPU via
# the `caller_gpu` arg (injected by the model's system prompt); anything matching
# an entry here is allowed. Override with PLAN_BUILD_SAFE_GPUS (comma-separated).
SAFE_GPUS = {
    g.strip().lower()
    for g in os.environ.get("PLAN_BUILD_SAFE_GPUS", "p100").split(",")
    if g.strip()
}
# Default V100 models to restore on `reset_models` (the daily base state). Warming
# these evicts any planner/coder (`big`/`coder-next`) left resident. Override with
# PLAN_BUILD_DEFAULT_MODELS (comma-separated).
DEFAULT_MODELS = [
    m.strip()
    for m in os.environ.get("PLAN_BUILD_DEFAULT_MODELS", "coding,chat").split(",")
    if m.strip()
]
# `fast_plan_and_build` only uses the resident daily V100 models (`chat`+`coding`),
# so it never evicts them and may also be called from those V100 models -- not just
# the P100 `fast` model. Override with PLAN_BUILD_FAST_SAFE_GPUS (comma-separated).
FAST_SAFE_GPUS = {
    g.strip().lower()
    for g in os.environ.get("PLAN_BUILD_FAST_SAFE_GPUS", "p100,v100").split(",")
    if g.strip()
}

# Human-readable "who may call this" clauses, embedded in guard refusal messages.
_STRICT_WHO = (
    "a model that lives exclusively on the P100 (the `fast` chat model) -- this tool "
    "swaps `big`/`coder-next` onto the V100s and would otherwise evict the caller and "
    "break the conversation"
)
_FAST_WHO = (
    "the P100 `fast` model or a V100 daily model (`chat`/`coding`) -- this tool uses the "
    "resident `chat`+`coding` models in place and won't evict them"
)

mcp = FastMCP("plan-build")


def _gpu_guard(
    caller_gpu: str,
    allowed: set[str] | None = None,
    who: str = _STRICT_WHO,
) -> str | None:
    """Return a refusal message if the caller's GPU isn't allowed, else None.

    Callers must report their GPU/tier via `caller_gpu` (injected by the model's
    system prompt). `allowed` defaults to `SAFE_GPUS` (P100-only); tools that don't
    evict the daily V100 models can pass a wider set (e.g. `FAST_SAFE_GPUS`).
    """
    allowed = SAFE_GPUS if allowed is None else allowed
    val = (caller_gpu or "").strip().lower()
    if not val:
        return (
            f"⚠️ Refused: this tool must be called from {who}. The caller did not report "
            "its GPU (`caller_gpu`). Set the model's system prompt to always pass "
            "`caller_gpu` on plan-build calls, and only enable this tool on the "
            "appropriate model(s)."
        )
    if any(safe in val for safe in allowed):
        return None
    return (
        f"⚠️ Refused: the caller reported caller_gpu='{caller_gpu}', which is not "
        f"permitted here. This tool must be called from {who}."
    )


def _chat(model: str, prompt: str, max_tokens: int) -> dict:
    """Call the LiteLLM gateway; return {content, finish, tokens}."""
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "top_p": 1.0,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        f"{LITELLM_BASE}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:  # surface gateway errors readably
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"LiteLLM HTTP {e.code} for model '{model}': {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach LiteLLM at {LITELLM_BASE} ({e.reason}). "
            "Is the gateway up and is host.docker.internal mapped?"
        ) from None
    choice = data["choices"][0]
    msg = choice.get("message", {})
    return {
        "content": (msg.get("content") or "").strip(),
        "finish": choice.get("finish_reason"),
        "tokens": data.get("usage", {}).get("completion_tokens"),
    }


def _plan_prompt(task: str) -> str:
    return (
        "You are a senior software engineer writing an implementation plan for another "
        "engineer who will write the code. Do NOT write the full implementation yourself. "
        "Produce a precise, actionable plan that removes ambiguity:\n"
        "1. Restate the requirements and call out any that are under-specified, choosing "
        "sensible, explicit defaults.\n"
        "2. The data structures / interfaces (with exact shapes and signatures).\n"
        "3. A numbered, ordered task list to implement each part.\n"
        "4. The tricky edge cases and how to handle them.\n"
        "5. How correctness should be verified (key test scenarios).\n"
        "Be thorough but concise. Output the plan as Markdown.\n\n"
        f"TASK:\n{task}"
    )


def _impl_prompt(spec: str) -> str:
    return (
        "Implement the following completely and correctly. Follow it precisely, including "
        "any edge-case handling it specifies. Output the final code in fenced code "
        "block(s), followed by a one-paragraph note on assumptions and how to run/test.\n\n"
        f"SPECIFICATION / PLAN:\n{spec}"
    )


@mcp.tool()
def make_plan(task: str, caller_gpu: str = "", planner_model: str = "big") -> str:
    """Design a detailed implementation plan for a coding task (planning only).

    Use to think through a non-trivial or under-specified task before writing
    code. Review/edit the returned plan, then pass it to `implement_spec` (or
    call `plan_and_build` to do both in one step).

    Args:
        task: Natural-language description of what to build.
        caller_gpu: REQUIRED. The GPU/card the calling model runs on (e.g.
            "p100"). Injected by your system prompt. This tool only runs when the
            caller lives exclusively on the P100 (the `fast` model), because it
            swaps `big`/`coder-next` onto the V100s and would otherwise evict the
            caller. Always pass this.
        planner_model: Model that writes the plan. 'big' (default, highest
            precision, slower) or 'chat' (reasoning MoE, faster) on the V100s, or
            'fast' (P100, quickest, weaker plans, no GPU swap).

    Returns:
        Markdown plan.

    Note: 'big' can take several minutes (deep reasoning) and swaps onto the V100s.
    """
    refusal = _gpu_guard(caller_gpu)
    if refusal:
        return refusal
    if not task or not task.strip():
        return "Error: `task` must be a non-empty description of what to build."
    plan = _chat(planner_model, _plan_prompt(task), max_tokens=24000)
    if plan["finish"] == "length":
        plan["content"] += "\n\n_(plan was truncated by the token limit)_"
    return (
        f"# Plan\n_by `{planner_model}`_\n\n**Task:** {task.strip()}\n\n"
        f"{plan['content']}\n\n"
        "> Next: review/edit this plan, then call `implement_spec` to build it."
    )


@mcp.tool()
def plan_and_build(
    task: str,
    caller_gpu: str = "",
    planner_model: str = "big",
    coder_model: str = "coder-next",
) -> str:
    """Plan a coding task with a reasoning model, then implement it with a coder.

    Best for non-trivial or under-specified tasks: the planner resolves ambiguity
    and designs the approach, then the coding model writes it.

    Args:
        task: Natural-language description of what to build.
        caller_gpu: REQUIRED. The GPU/card the calling model runs on (e.g.
            "p100"). Injected by your system prompt. This tool only runs when the
            caller lives exclusively on the P100 (the `fast` model), because it
            swaps `big`/`coder-next` onto the V100s and would otherwise evict the
            caller. Always pass this.
        planner_model: Model that writes the plan. Default 'big' (highest
            precision). Alternatives: 'chat' (faster reasoning) or 'fast' (P100,
            quickest, no GPU swap).
        coder_model: Coding model that implements the plan. Default 'coder-next'.

    Returns:
        Markdown containing both the plan and the implementation.

    Note: With the default 'big' planner, this can take several minutes (deep
    reasoning) plus a GPU swap from the planner to the coder. The 'fast' chat
    model stays resident on the P100, so your conversation keeps responding.
    """
    refusal = _gpu_guard(caller_gpu)
    if refusal:
        return refusal
    if not task or not task.strip():
        return "Error: `task` must be a non-empty description of what to build."
    plan = _chat(planner_model, _plan_prompt(task), max_tokens=24000)
    if plan["finish"] == "length":
        plan["content"] += "\n\n_(plan was truncated by the token limit)_"
    impl = _chat(coder_model, _impl_prompt(plan["content"]), max_tokens=8000)
    return (
        f"# Plan-and-build\n\n**Task:** {task.strip()}\n\n"
        f"## Plan\n_by `{planner_model}`_\n\n{plan['content']}\n\n"
        f"## Implementation\n_by `{coder_model}`_\n\n{impl['content']}\n"
    )


@mcp.tool()
def fast_plan_and_build(
    task: str,
    caller_gpu: str = "",
    planner_model: str = "chat",
    coder_model: str = "coding",
) -> str:
    """Quick plan + implement using the resident daily V100 models (no GPU swap).

    The interactive counterpart to `plan_and_build`: it plans with `chat`
    (Qwen3.6 MoE reasoner) and implements with `coding` (Qwen3.6-27B) -- the two
    models normally already resident together on the V100s (idx2 + idx1). Because
    it uses them in place, there is NO model swap, so it is fast and well-suited to
    interactive programming sessions.

    Prefer this for everyday/interactive tasks. Use `plan_and_build` (`big` +
    `coder-next`) instead for very complex or long-running / overnight work that
    needs maximum planning depth and the 80B coder.

    Args:
        task: Natural-language description of what to build.
        caller_gpu: REQUIRED. The GPU the calling model runs on ("p100" for the
            `fast` model, or "v100" for the `chat`/`coding` models). Injected by
            your system prompt. Because this tool only uses the resident
            `chat`/`coding` models, it may be called from the P100 `fast` model OR
            those V100 daily models.
        planner_model: Planner. Default 'chat'.
        coder_model: Coder. Default 'coding'.

    Returns:
        Markdown containing both the plan and the implementation.
    """
    refusal = _gpu_guard(caller_gpu, FAST_SAFE_GPUS, who=_FAST_WHO)
    if refusal:
        return refusal
    if not task or not task.strip():
        return "Error: `task` must be a non-empty description of what to build."
    plan = _chat(planner_model, _plan_prompt(task), max_tokens=24000)
    if plan["finish"] == "length":
        plan["content"] += "\n\n_(plan was truncated by the token limit)_"
    impl = _chat(coder_model, _impl_prompt(plan["content"]), max_tokens=8000)
    return (
        f"# Fast plan-and-build\n\n**Task:** {task.strip()}\n\n"
        f"## Plan\n_by `{planner_model}`_\n\n{plan['content']}\n\n"
        f"## Implementation\n_by `{coder_model}`_\n\n{impl['content']}\n"
    )


@mcp.tool()
def fast_make_plan(task: str, caller_gpu: str = "", planner_model: str = "chat") -> str:
    """Design an implementation plan using the resident `chat` model (no GPU swap).

    The interactive counterpart to `make_plan`: it plans with `chat` (Qwen3.6 MoE
    reasoner), a daily V100 model that stays resident, so there is no model swap.
    Review/edit the returned plan, then pass it to `fast_implement_spec` (or use
    `fast_plan_and_build` to do both in one step). Use `make_plan` (`big`) instead
    for very complex or long-running work that needs maximum planning depth.

    Args:
        task: Natural-language description of what to build.
        caller_gpu: REQUIRED. The GPU the calling model runs on ("p100" for the
            `fast` model, or "v100" for the `chat`/`coding` models). Injected by
            your system prompt. Because this tool only uses the resident `chat`
            model, it may be called from the P100 `fast` model OR those V100 daily
            models.
        planner_model: Planner. Default 'chat'.

    Returns:
        Markdown plan.
    """
    refusal = _gpu_guard(caller_gpu, FAST_SAFE_GPUS, who=_FAST_WHO)
    if refusal:
        return refusal
    if not task or not task.strip():
        return "Error: `task` must be a non-empty description of what to build."
    plan = _chat(planner_model, _plan_prompt(task), max_tokens=24000)
    if plan["finish"] == "length":
        plan["content"] += "\n\n_(plan was truncated by the token limit)_"
    return (
        f"# Plan\n_by `{planner_model}`_\n\n**Task:** {task.strip()}\n\n"
        f"{plan['content']}\n\n"
        "> Next: review/edit this plan, then call `fast_implement_spec` to build it."
    )


@mcp.tool()
def implement_spec(spec: str, caller_gpu: str = "", coder_model: str = "coder-next") -> str:
    """Implement code directly from a specification or plan (no planning step).

    Use when you already have a clear spec/plan -- e.g. one produced by
    `make_plan` (optionally edited), or a detailed spec you wrote yourself.

    Args:
        spec: The specification or implementation plan to build from.
        caller_gpu: REQUIRED. The GPU/card the calling model runs on (e.g.
            "p100"). Injected by your system prompt. This tool only runs when the
            caller lives exclusively on the P100 (the `fast` model), because it
            swaps `coder-next` onto the V100s and would otherwise evict the
            caller. Always pass this.
        coder_model: Coding model that implements it. Default 'coder-next'.

    Returns:
        Markdown with the implementation produced by the coding model.
    """
    refusal = _gpu_guard(caller_gpu)
    if refusal:
        return refusal
    if not spec or not spec.strip():
        return "Error: `spec` must be a non-empty specification or plan."
    impl = _chat(coder_model, _impl_prompt(spec), max_tokens=8000)
    return f"# Implementation\n_by `{coder_model}`_\n\n{impl['content']}\n"


@mcp.tool()
def fast_implement_spec(spec: str, caller_gpu: str = "", coder_model: str = "coding") -> str:
    """Implement code from a spec/plan using the resident `coding` model (no swap).

    The interactive counterpart to `implement_spec`: it implements with `coding`
    (Qwen3.6-27B), a daily V100 model that stays resident, so there is no model
    swap. Use when you already have a clear spec/plan -- e.g. one from
    `fast_make_plan` (optionally edited), or a detailed spec you wrote yourself.
    Use `implement_spec` (`coder-next`, 80B) instead for the largest/most complex
    implementations.

    Args:
        spec: The specification or implementation plan to build from.
        caller_gpu: REQUIRED. The GPU the calling model runs on ("p100" for the
            `fast` model, or "v100" for the `chat`/`coding` models). Injected by
            your system prompt. Because this tool only uses the resident `coding`
            model, it may be called from the P100 `fast` model OR those V100 daily
            models.
        coder_model: Coder. Default 'coding'.

    Returns:
        Markdown with the implementation produced by the coding model.
    """
    refusal = _gpu_guard(caller_gpu, FAST_SAFE_GPUS, who=_FAST_WHO)
    if refusal:
        return refusal
    if not spec or not spec.strip():
        return "Error: `spec` must be a non-empty specification or plan."
    impl = _chat(coder_model, _impl_prompt(spec), max_tokens=8000)
    return f"# Implementation\n_by `{coder_model}`_\n\n{impl['content']}\n"


@mcp.tool()
def reset_models(caller_gpu: str = "") -> str:
    """Reset the V100s to their default daily models (free the planner/coder).

    Call this when the user is finished planning/building and wants to return to
    the base state -- e.g. they say "stop", "reset", "done", "free the GPUs", or
    "go back to normal". It warms the default coding models back onto the V100s,
    which evicts any planner/coder (`big`/`coder-next`) left resident from earlier
    plan-build calls. The `fast` chat model on the P100 is unaffected, so this
    conversation keeps responding.

    Args:
        caller_gpu: REQUIRED. The GPU/card the calling model runs on (e.g.
            "p100"). Injected by your system prompt. Same P100-exclusive rule as
            the other tools.

    Returns:
        A short status line listing which models were restored.
    """
    refusal = _gpu_guard(caller_gpu)
    if refusal:
        return refusal
    if not DEFAULT_MODELS:
        return "No default models configured (PLAN_BUILD_DEFAULT_MODELS is empty)."
    restored, failed = [], []
    for m in DEFAULT_MODELS:
        try:
            _chat(m, "ok", max_tokens=1)  # tiny warmup request triggers the load
            restored.append(m)
        except RuntimeError as e:
            failed.append(f"`{m}` ({e})")
    parts = []
    if restored:
        parts.append("✅ Reset the V100s to the default models: " + ", ".join(f"`{m}`" for m in restored) + ".")
    if failed:
        parts.append("⚠️ Failed to restore: " + "; ".join(failed) + ".")
    parts.append("The `fast` model (P100) was unaffected.")
    return " ".join(parts)


if __name__ == "__main__":
    mcp.run()

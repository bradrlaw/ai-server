#!/usr/bin/env python3
"""plan-build MCP server for /srv/ai.

Exposes a planner -> coder pipeline as MCP tools, surfaced to Open WebUI through
mcpo (OpenAPI). The general chat model (`fast`, on the P100) calls these tools
and relays their output; the tools do the heavy lifting on the V100s:

  * `make_plan`       - a reasoning model designs a detailed plan (planning only)
  * `plan_and_build`  - plan with a reasoning model, then implement with a coder
  * `implement_spec`  - implement directly from a given spec/plan (no planning)

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

mcp = FastMCP("plan-build")


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
def make_plan(task: str, planner_model: str = "big") -> str:
    """Design a detailed implementation plan for a coding task (planning only).

    Use to think through a non-trivial or under-specified task before writing
    code. Review/edit the returned plan, then pass it to `implement_spec` (or
    call `plan_and_build` to do both in one step).

    Args:
        task: Natural-language description of what to build.
        planner_model: Model that writes the plan. 'big' (default, highest
            precision, slower) or 'chat' (reasoning MoE, faster) on the V100s, or
            'fast' (P100, quickest, weaker plans, no GPU swap).

    Returns:
        Markdown plan.

    Note: 'big' can take several minutes (deep reasoning) and swaps onto the V100s.
    """
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
    planner_model: str = "big",
    coder_model: str = "coder-next",
) -> str:
    """Plan a coding task with a reasoning model, then implement it with a coder.

    Best for non-trivial or under-specified tasks: the planner resolves ambiguity
    and designs the approach, then the coding model writes it.

    Args:
        task: Natural-language description of what to build.
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
def implement_spec(spec: str, coder_model: str = "coder-next") -> str:
    """Implement code directly from a specification or plan (no planning step).

    Use when you already have a clear spec/plan -- e.g. one produced by
    `make_plan` (optionally edited), or a detailed spec you wrote yourself.

    Args:
        spec: The specification or implementation plan to build from.
        coder_model: Coding model that implements it. Default 'coder-next'.

    Returns:
        Markdown with the implementation produced by the coding model.
    """
    if not spec or not spec.strip():
        return "Error: `spec` must be a non-empty specification or plan."
    impl = _chat(coder_model, _impl_prompt(spec), max_tokens=8000)
    return f"# Implementation\n_by `{coder_model}`_\n\n{impl['content']}\n"


if __name__ == "__main__":
    mcp.run()

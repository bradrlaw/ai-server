#!/usr/bin/env python3
"""llama-swap mode-switch MCP server for /srv/ai.

Exposes the config-mode switcher (scripts/llama-swap-mode.py) as MCP tools so a
client (Open WebUI via mcpo, or a Copilot BYOK session over streamable-http) can
list, inspect, and switch the llama-swap serving mode without shelling into the
box.

  * `list_modes`   - available modes + which one is active
  * `current_mode` - active mode + effective per-model config (parallel/ctx)
  * `show_mode`    - effective per-model config a given mode WOULD produce
  * `set_mode`     - render + activate a mode, warming its models

This MUST run natively on the HOST (it edits config/llama-swap.yaml and calls
llama-swap on 127.0.0.1:9090), so it is served over streamable-http (like
comfyui-mcp) and proxied to Open WebUI by mcpo — never launched inside the mcpo
container. Switching a mode only changes a few --parallel / concurrencyLimit
knobs + the preload list; llama-swap's -watch-config reloads the file, so no
restart / sudo is needed.

Runtime: scripts/llama-swap-mode-mcp.service runs it under the comfyui-mcp venv
(has `mcp`), binding 0.0.0.0:9120. It invokes the switcher with a host Python
that has PyYAML (MODE_SWITCH_PY, default /usr/bin/python3).
"""
from __future__ import annotations

import json
import os
import subprocess

from mcp.server.fastmcp import FastMCP

# Host interpreter with PyYAML (the switcher needs it); NOT the mcp venv python.
MODE_SWITCH_PY = os.environ.get("MODE_SWITCH_PY", "/usr/bin/python3")
MODE_SWITCH = os.environ.get(
    "MODE_SWITCH_SCRIPT", "/srv/ai/scripts/llama-swap-mode.py")
# `set_mode` warms models, which can trigger a multi-GB (re)load — allow time.
TIMEOUT = float(os.environ.get("MODE_SWITCH_TIMEOUT", "900"))

mcp = FastMCP("llama-swap-mode")


def _run(args: list[str], timeout: float = 60.0) -> dict:
    """Invoke the switcher with --json and parse its stdout."""
    cmd = [MODE_SWITCH_PY, MODE_SWITCH, "--json", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"mode switch timed out after {timeout}s"}
    if p.returncode != 0:
        return {"error": (p.stderr or p.stdout or "unknown error").strip()}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"error": f"unparseable output: {p.stdout[:400]}"}


@mcp.tool()
def list_modes() -> str:
    """List the available llama-swap serving modes and mark the active one.

    Returns JSON: {"current": <name>, "modes": [{name, label, description}, ...]}.
    Use this to discover what modes exist before calling set_mode.
    """
    return json.dumps(_run(["list"]))


@mcp.tool()
def current_mode() -> str:
    """Report the currently active llama-swap mode and its effective config.

    Returns JSON: {"current": <name>, "effective": {model: {parallel, ctx,
    ctx_per_slot, concurrencyLimit, gpus}}}.
    """
    return json.dumps(_run(["current"]))


@mcp.tool()
def show_mode(mode: str) -> str:
    """Preview the effective per-model config a mode WOULD produce (no change).

    `mode` is a mode name from list_modes (e.g. "daily", "heavy-coding").
    Returns JSON: {"mode": <name>, "effective": {model: {parallel, ctx, ...}}}.
    """
    return json.dumps(_run(["show", mode]))


@mcp.tool()
def set_mode(mode: str) -> str:
    """Activate a llama-swap serving mode, warming its models.

    `mode` is a mode name from list_modes (e.g. "daily", "heavy-coding").
    Renders config/llama-swap.yaml from the base + the mode overlay; llama-swap
    reloads it automatically (no restart). Then warms the mode's models, which
    may take up to a few minutes for large (re)loads. Returns JSON with the new
    mode, warm results, and effective per-model config.
    """
    return json.dumps(_run(["set", mode], timeout=TIMEOUT))


if __name__ == "__main__":
    # Default transport is stdio; the AI server runs it as an unauthenticated
    # streamable-http service (scripts/llama-swap-mode-mcp.service) proxied to
    # Open WebUI by mcpo and usable by Copilot BYOK over HTTP.
    _transport = os.environ.get("MODE_MCP_TRANSPORT", "stdio").strip().lower()
    if _transport in ("streamable-http", "streamable_http", "http"):
        mcp.settings.host = os.environ.get("MODE_MCP_HOST", "0.0.0.0")
        _port = os.environ.get("MODE_MCP_PORT", "").strip()
        if _port:
            mcp.settings.port = int(_port)
        # Unauthenticated bridge on the private LAN/Tailscale — disable FastMCP's
        # localhost-only DNS-rebinding guard so LAN/Tailscale clients aren't
        # rejected with 421 (same posture as plan-build / comfyui-mcp).
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
            allowed_origins=["*"],
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()

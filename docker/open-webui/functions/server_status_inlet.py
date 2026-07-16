"""
title: Server Status (new-chat banner)
author: bradrlaw (ai-server)
description: On the first message of a new chat, fetch live server state and surface it
    so the user sees "what's currently running" up front. Pulls loaded models from
    llama-swap (/running, /v1/models) and active image/video jobs from the ComfyUI
    instances (/queue). Optionally enriches with GPU/host data from a status JSON API
    (the detailed web status page) if one is configured. By default it injects a compact
    system-context block so the assistant opens its first reply with the status, and also
    emits a transient status banner via the event emitter. All network calls are best
    effort with short timeouts — a down service never blocks the chat.
version: 1.0.0
required_open_webui_version: 0.5.0
"""

import asyncio
from typing import Awaitable, Callable, Optional

import requests
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(
            default=True,
            description="Show live server status at the start of each new chat.",
        )
        llama_swap_url: str = Field(
            default="http://host.docker.internal:9090",
            description="llama-swap management base URL (reachable from the OWUI container).",
        )
        comfyui_urls: str = Field(
            default="open=http://host.docker.internal:8188",
            description=(
                "Comma-separated label=url pairs for ComfyUI instances to report queue "
                "depth for. The password-locked instance (:8189) can't be polled without "
                "auth, so it's omitted by default. Set to empty to skip ComfyUI."
            ),
        )
        status_api_url: str = Field(
            default="",
            description=(
                "Optional JSON status endpoint (the detailed web status page). If it "
                "returns an object, its 'summary' string and/or 'gpus' list are appended."
            ),
        )
        inject_mode: str = Field(
            default="both",
            description="How to surface status: 'system' (context block), 'banner' (event chip), or 'both'.",
        )
        request_timeout: float = Field(
            default=2.5, description="Per-request HTTP timeout in seconds."
        )

    def __init__(self):
        self.valves = self.Valves()

    # ---- data collection -------------------------------------------------

    def _get_json(self, url: str):
        try:
            r = requests.get(url, timeout=self.valves.request_timeout)
            if r.ok:
                return r.json()
        except Exception:
            pass
        return None

    def _models_status(self) -> Optional[str]:
        base = (self.valves.llama_swap_url or "").rstrip("/")
        if not base:
            return None
        running = self._get_json(f"{base}/running") or {}
        rows = running.get("running") or []
        loaded = [
            f"{m.get('model', '?')} ({m.get('state', '?')})"
            for m in rows
            if isinstance(m, dict)
        ]
        catalog = self._get_json(f"{base}/v1/models") or {}
        total = len(catalog.get("data") or []) if isinstance(catalog, dict) else 0
        if loaded:
            line = "**Loaded models:** " + ", ".join(loaded)
        else:
            line = "**Loaded models:** none (idle — first request will load one)"
        if total:
            line += f"  ·  {total} available"
        return line

    def _comfy_status(self) -> Optional[str]:
        spec = (self.valves.comfyui_urls or "").strip()
        if not spec:
            return None
        parts = []
        for pair in spec.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            label, url = pair.split("=", 1)
            label = label.strip()
            try:
                r = requests.get(
                    f"{url.strip().rstrip('/')}/queue", timeout=self.valves.request_timeout
                )
            except Exception:
                parts.append(f"{label}: unreachable")
                continue
            if r.status_code in (401, 403):
                parts.append(f"{label}: locked")
                continue
            if not r.ok:
                parts.append(f"{label}: unreachable")
                continue
            try:
                q = r.json()
            except Exception:
                parts.append(f"{label}: unreachable")
                continue
            running = len(q.get("queue_running") or [])
            pending = len(q.get("queue_pending") or [])
            if running or pending:
                parts.append(f"{label}: {running} running, {pending} queued")
            else:
                parts.append(f"{label}: idle")
        if not parts:
            return None
        return "**ComfyUI:** " + "  ·  ".join(parts)

    def _extra_status(self) -> Optional[str]:
        url = (self.valves.status_api_url or "").strip()
        if not url:
            return None
        data = self._get_json(url)
        if not isinstance(data, dict):
            return None
        lines = []
        if isinstance(data.get("summary"), str) and data["summary"].strip():
            lines.append(data["summary"].strip())
        gpus = data.get("gpus")
        if isinstance(gpus, list) and gpus:
            g = []
            for gpu in gpus:
                if not isinstance(gpu, dict):
                    continue
                idx = gpu.get("index", "?")
                util = gpu.get("util")
                mem_used = gpu.get("mem_used")
                mem_total = gpu.get("mem_total")
                power = gpu.get("power")
                temp = gpu.get("temp")
                bits = [f"GPU{idx}"]
                if util is not None:
                    bits.append(f"{util}% util")
                if mem_used is not None and mem_total is not None:
                    bits.append(f"{mem_used}/{mem_total} MB")
                if power is not None:
                    bits.append(f"{power} W")
                if temp is not None:
                    bits.append(f"{temp}°C")
                g.append(" ".join(bits))
            if g:
                lines.append("**GPUs:** " + "  ·  ".join(g))
        return "\n".join(lines) if lines else None

    def _build_status(self) -> Optional[str]:
        sections = [
            self._models_status(),
            self._comfy_status(),
            self._extra_status(),
        ]
        sections = [s for s in sections if s]
        if not sections:
            return None
        return "\n".join(sections)

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _is_new_chat(body: dict) -> bool:
        msgs = body.get("messages") or []
        user_turns = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")
        return user_turns <= 1

    # ---- OWUI hook -------------------------------------------------------

    async def inlet(
        self,
        body: dict,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __user__: dict = None,
    ) -> dict:
        if not self.valves.enabled:
            return body
        if not self._is_new_chat(body):
            return body

        status = await asyncio.to_thread(self._build_status)
        if not status:
            return body

        mode = (self.valves.inject_mode or "both").lower()

        if mode in ("banner", "both") and __event_emitter__:
            first_line = status.splitlines()[0].replace("**", "")
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {"description": f"Server status — {first_line}", "done": True},
                    }
                )
            except Exception:
                pass

        if mode in ("system", "both"):
            block = (
                "Live server status at the start of this chat (from the AI server "
                "monitoring hooks). Open your first reply with a one-line greeting, then "
                "present this status as a compact markdown block, then help the user:\n\n"
                + status
            )
            messages = body.setdefault("messages", [])
            messages.insert(0, {"role": "system", "content": block})

        return body

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
            default="http://host.docker.internal:9095/status.json",
            description=(
                "Primary source: JSON status endpoint served by the host-side status "
                "service (scripts/server-status-service.py). When reachable it provides "
                "models, ComfyUI, and GPU data. If empty/unreachable, the filter falls "
                "back to the direct llama-swap/ComfyUI URLs below."
            ),
        )
        inject_mode: str = Field(
            default="banner",
            description=(
                "How to surface status: 'banner' (event chip, no model message — "
                "default), 'system' (inject a context block so the model opens with it), "
                "or 'both'."
            ),
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
        running = self._get_json(f"{base}/running")
        if running is None:
            return "**Loaded models:** llama-swap unreachable"
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

    def _render_from_api(self, data: dict) -> Optional[str]:
        """Render the full status block from the host-side status service JSON."""
        lines = []

        models = data.get("models") or {}
        if isinstance(models, dict):
            if models.get("reachable") is False:
                lines.append("**Loaded models:** llama-swap unreachable")
            else:
                loaded = models.get("loaded") or []
                names = [
                    f"{m.get('model', '?')} ({m.get('state', '?')})"
                    for m in loaded
                    if isinstance(m, dict)
                ]
                if names:
                    line = "**Loaded models:** " + ", ".join(names)
                else:
                    line = "**Loaded models:** none (idle — first request will load one)"
                total = models.get("available")
                if total:
                    line += f"  ·  {total} available"
                lines.append(line)

        gpus = data.get("gpus")
        if isinstance(gpus, list) and gpus:
            g = []
            for gpu in gpus:
                if not isinstance(gpu, dict):
                    continue
                bits = [f"GPU{gpu.get('index', '?')}"]
                if gpu.get("util") is not None:
                    bits.append(f"{gpu['util']}%")
                if gpu.get("mem_used") is not None and gpu.get("mem_total"):
                    bits.append(f"{gpu['mem_used']}/{gpu['mem_total']}MB")
                if gpu.get("power") is not None:
                    bits.append(f"{gpu['power']}W")
                if gpu.get("temp") is not None:
                    bits.append(f"{gpu['temp']}°C")
                g.append(" ".join(bits))
            if g:
                lines.append("**GPUs:** " + "  ·  ".join(g))

        comfyui = data.get("comfyui")
        if isinstance(comfyui, list) and comfyui:
            parts = []
            for c in comfyui:
                if not isinstance(c, dict):
                    continue
                state = c.get("state", "?")
                if state == "busy":
                    parts.append(
                        f"{c.get('label', '?')}: {c.get('running', 0)} running, "
                        f"{c.get('pending', 0)} queued"
                    )
                else:
                    parts.append(f"{c.get('label', '?')}: {state}")
            if parts:
                lines.append("**ComfyUI:** " + "  ·  ".join(parts))

        return "\n".join(lines) if lines else None

    def _build_status(self) -> Optional[str]:
        # Prefer the host-side status service (reachable from the container and the
        # only source that can report GPU stats).
        api = (self.valves.status_api_url or "").strip()
        if api:
            data = self._get_json(api)
            if isinstance(data, dict):
                rendered = self._render_from_api(data)
                if rendered:
                    return rendered
        # Fallback: query llama-swap / ComfyUI directly (no GPU data).
        sections = [self._models_status(), self._comfy_status()]
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

        mode = (self.valves.inject_mode or "banner").lower()

        if mode in ("banner", "both") and __event_emitter__:
            # Compact the multi-line markdown into one banner line (no model message).
            banner = "  ·  ".join(
                ln.replace("**", "").strip() for ln in status.splitlines() if ln.strip()
            )
            try:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {"description": f"Server status — {banner}", "done": True},
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

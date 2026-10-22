"""
title: Server Status (new-chat banner)
author: bradrlaw (ai-server)
description: On the first message of a new chat, fetch live server state and surface it
    so the user sees "what's currently running" up front. The source is the host-side
    status service's JSON API (loaded models, GPU/host data, ComfyUI queues). If that is
    unreachable it falls back to polling the ComfyUI instances (/queue) directly, plus
    llama-swap (/running, /v1/models) when a reachable llama-swap URL is configured. By
    default it shows a multi-line status card at the top of the first reply; it can also
    inject a system-context block so the assistant opens its first reply with the status.
    All network calls are best effort with short timeouts — a down service never blocks
    the chat.
version: 1.2.0
required_open_webui_version: 0.9.3
"""

import asyncio
import html
import time
from typing import Awaitable, Callable, List, NamedTuple, Optional, Tuple

import requests
from pydantic import BaseModel, Field

_SEP = "  ·  "


class _Row(NamedTuple):
    """One status section, e.g. ("GPUs", ["GPU0 0% ...", "GPU1 ..."])."""

    label: str
    items: List[str]
    stack: bool = False  # card only: one item per line instead of wrapping inline


# Why a card: OWUI draws a "status" event's text clamped to one line (StatusItem.svelte,
# `line-clamp-1`), so the full status ended in "…" with no way to expand it. An "embeds"
# event is drawn above the reply by OWUI's FullHeightIframe as a sandboxed srcdoc iframe
# (scripts allowed, no same-origin); it wraps, is saved with the chat, and is never sent
# to the model. Being sandboxed, the card posts its own height ("iframe:height") and
# can't see OWUI's theme class, so it uses translucent fills and greys that read on both
# themes; prefers-color-scheme tracks OWUI when its theme is "System".
_CARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;padding:0;background:transparent}
body{font:13.5px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
color:#6e6e6e;-webkit-text-size-adjust:100%}
@media (prefers-color-scheme:dark){body{color:#9a9a9a}}
#card{box-sizing:border-box;border:1px solid rgba(128,128,128,.3);border-radius:12px;
padding:8px 12px;background:rgba(128,128,128,.07)}
.hd{font-weight:600;margin-bottom:4px}
.hd time{font-weight:400;opacity:.85}
.grid{display:grid;grid-template-columns:max-content minmax(0,1fr);column-gap:14px;row-gap:3px}
.k{font-weight:600;white-space:nowrap}
.v span{display:inline-block}
.v span:not(:last-child)::after{content:"·";padding:0 .5em;opacity:.7}
.v.stack span{display:block}
.v.stack span::after{content:none}
@media (max-width:460px){.grid{grid-template-columns:minmax(0,1fr);row-gap:0}
.k:not(:first-child){margin-top:4px}}
</style></head><body>
<div id="card"><div class="hd">Server status <time data-ts="@@TS@@">· @@WHEN@@</time></div>
<div class="grid">@@ROWS@@</div></div>
<script>
(function () {
  var card = document.getElementById("card");
  var t = document.querySelector("time[data-ts]");
  var ts = t && Number(t.getAttribute("data-ts"));
  if (ts) {
    try {
      t.textContent = "· " + new Date(ts * 1000).toLocaleString([], {
        month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
      });
    } catch (e) {}
  }
  document.documentElement.style.overflow = "hidden";
  function post() {
    try {
      var h = Math.ceil(card.getBoundingClientRect().height) + 2;
      parent.postMessage({ type: "iframe:height", height: h }, "*");
    } catch (e) {}
  }
  if (window.ResizeObserver) new ResizeObserver(post).observe(card);
  addEventListener("load", post);
  setTimeout(post, 300);
  post();
})();
</script></body></html>"""


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(
            default=True,
            description="Show live server status at the start of each new chat.",
        )
        llama_swap_url: str = Field(
            default="",
            description=(
                "Fallback only: llama-swap management base URL, polled when the status API "
                "is unreachable. Empty by default because llama-swap binds 127.0.0.1:9090, "
                "which the OWUI container can't reach; the status API reports models instead."
            ),
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
                "back to the direct ComfyUI (and llama-swap, if set) URLs."
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
        banner_style: str = Field(
            default="card",
            description=(
                "Look of the banner: 'card' (multi-line status card at the top of the "
                "first reply — default) or 'line' (OWUI's one-line status text, which "
                "OWUI cuts off with '…' when it doesn't fit)."
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

    def _models_row(self) -> List[_Row]:
        base = (self.valves.llama_swap_url or "").rstrip("/")
        if not base:
            return []
        running = self._get_json(f"{base}/running")
        if running is None:
            return [_Row("Loaded models", ["llama-swap unreachable"])]
        catalog = self._get_json(f"{base}/v1/models") or {}
        catalog_entries = catalog.get("data") or [] if isinstance(catalog, dict) else []
        names_by_id = {
            c.get("id"): c.get("name")
            for c in catalog_entries
            if isinstance(c, dict) and c.get("id")
        }
        rows = running.get("running") or []
        loaded_ids = set()
        loaded = []
        for m in rows:
            if not isinstance(m, dict):
                continue
            mid = m.get("model", "?")
            loaded_ids.add(mid)
            display = names_by_id.get(mid)
            loaded.append(
                f"{mid}" + (f" — {display}" if display else "") + f" ({m.get('state', '?')})"
            )
        items = [", ".join(loaded) if loaded else "none (idle — first request will load one)"]
        result = [_Row("Loaded models", items)]
        avail_items = [
            f"{c.get('id', '?')}" + (f" — {c['name']}" if c.get("name") else "")
            for c in catalog_entries
            if isinstance(c, dict) and c.get("id") not in loaded_ids
        ]
        if avail_items:
            result.append(_Row("Available models", avail_items, stack=True))
        return result

    def _comfy_row(self) -> Optional[_Row]:
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
        return _Row("ComfyUI", parts)

    def _rows_from_api(self, data: dict) -> List[_Row]:
        """Status rows from the host-side status service JSON."""
        rows: List[_Row] = []

        models = data.get("models") or {}
        if isinstance(models, dict):
            if models.get("reachable") is False:
                rows.append(_Row("Loaded models", ["llama-swap unreachable"]))
            else:
                loaded = models.get("loaded") or []
                names = [
                    f"{m.get('model', '?')}"
                    + (f" — {m['display_name']}" if m.get("display_name") else "")
                    + f" ({m.get('state', '?')})"
                    for m in loaded
                    if isinstance(m, dict)
                ]
                items = [
                    ", ".join(names) if names else "none (idle — first request will load one)"
                ]
                rows.append(_Row("Loaded models", items))
                available = models.get("available")
                if isinstance(available, list) and available:
                    avail_items = [
                        f"{a.get('id', '?')}"
                        + (f" — {a['name']}" if isinstance(a, dict) and a.get("name") else "")
                        for a in available
                        if isinstance(a, dict)
                    ]
                    if avail_items:
                        rows.append(_Row("Available models", avail_items, stack=True))

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
                rows.append(_Row("GPUs", g, stack=True))

        host = data.get("host")
        if isinstance(host, dict):
            hp = []
            if host.get("cpu_pct") is not None:
                cpu = f"CPU {host['cpu_pct']:.0f}%"
                load = host.get("load")
                if isinstance(load, list) and load:
                    cpu += f" (load {' / '.join(str(x) for x in load)})"
                hp.append(cpu)
            mem = host.get("mem")
            if isinstance(mem, dict) and mem.get("total_mb"):
                hp.append(
                    f"RAM {mem.get('used_mb', 0) / 1024:.1f}/"
                    f"{mem['total_mb'] / 1024:.1f}GB ({mem.get('used_pct', '?')}%)"
                )
            for dk in host.get("disk") or []:
                if isinstance(dk, dict) and dk.get("total_gb"):
                    hp.append(
                        f"disk {dk.get('path', '?')} {dk.get('used_gb', 0):.0f}/"
                        f"{dk['total_gb']:.0f}GB ({dk.get('used_pct', '?')}%)"
                    )
            if hp:
                rows.append(_Row("Host", hp))

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
                rows.append(_Row("ComfyUI", parts))

        return rows

    def _build_rows(self) -> Tuple[List[_Row], int]:
        """Status rows plus the epoch time they describe ([] when there's nothing)."""
        # Prefer the host-side status service (reachable from the container and the
        # only source that can report GPU stats).
        api = (self.valves.status_api_url or "").strip()
        api_down = False
        if api:
            data = self._get_json(api)
            if isinstance(data, dict):
                rows = self._rows_from_api(data)
                if rows:
                    ts = data.get("timestamp")
                    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                        ts = time.time()
                    return rows, int(ts)
            else:
                api_down = True
        # Fallback: query llama-swap / ComfyUI directly (no GPU data).
        rows = [*self._models_row(), *([r] if (r := self._comfy_row()) else [])]
        if api_down and not (self.valves.llama_swap_url or "").strip():
            # Without llama-swap there's no model line, so say why it's missing.
            rows.insert(0, _Row("Status service", ["unreachable (no model/GPU data)"]))
        return rows, int(time.time())

    # ---- rendering -------------------------------------------------------

    @staticmethod
    def _markdown(rows: List[_Row]) -> str:
        return "\n".join(f"**{r.label}:** {_SEP.join(r.items)}" for r in rows)

    @staticmethod
    def _one_line(rows: List[_Row]) -> str:
        return _SEP.join(f"{r.label}: {_SEP.join(r.items)}" for r in rows)

    @staticmethod
    def _card(rows: List[_Row], ts: int) -> str:
        cells = []
        for r in rows:
            spans = "".join(f"<span>{html.escape(i)}</span>" for i in r.items)
            cls = "v stack" if r.stack else "v"
            cells.append(f'<div class="k">{html.escape(r.label)}</div><div class="{cls}">{spans}</div>')
        # Shown only if the card's script can't run; the script swaps in browser-local time.
        when = time.strftime("%b %d %H:%M UTC", time.gmtime(ts))
        # Rows last, so status text can never be taken for a placeholder.
        return (
            _CARD_HTML.replace("@@TS@@", str(ts))
            .replace("@@WHEN@@", when)
            .replace("@@ROWS@@", "".join(cells))
        )

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

        try:
            rows, ts = await asyncio.to_thread(self._build_rows)
        except Exception:
            return body  # e.g. a malformed status payload: skip the banner, keep the chat
        if not rows:
            return body

        mode = (self.valves.inject_mode or "banner").lower()

        if mode in ("banner", "both") and __event_emitter__:
            if (self.valves.banner_style or "").strip().lower() == "line":
                event = {
                    "type": "status",
                    "data": {
                        "description": f"Server status — {self._one_line(rows)}",
                        "done": True,
                    },
                }
            else:
                # replace=True stores the list as given instead of first reading the
                # message to append to its (still empty) embeds.
                event = {
                    "type": "embeds",
                    "data": {"embeds": [self._card(rows, ts)], "replace": True},
                }
            try:
                await __event_emitter__(event)
            except Exception:
                pass

        if mode in ("system", "both"):
            block = (
                "Live server status at the start of this chat (from the AI server "
                "monitoring hooks). Open your first reply with a one-line greeting, then "
                "present this status as a compact markdown block, then help the user:\n\n"
                + self._markdown(rows)
            )
            messages = body.setdefault("messages", [])
            messages.insert(0, {"role": "system", "content": block})

        return body

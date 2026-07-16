#!/usr/bin/env python3
"""AI-server status service.

A small, dependency-free (Python stdlib only) HTTP service that aggregates live
state from the local, loopback-bound components and exposes it where bridge
containers (Open WebUI) and browsers can reach it:

  GET /status.json  -> machine-readable status (consumed by the OWUI status filter)
  GET /             -> minimal auto-refreshing HTML dashboard (foundation for a
                       richer page later)
  GET /healthz      -> "ok"

Sources (all read locally on the host):
  - llama-swap   : http://127.0.0.1:9090/running  and  /v1/models
  - ComfyUI      : http://127.0.0.1:8188/queue     (open, no auth)
                   http://127.0.0.1:8189/queue     (secure, login-gated -> "locked")
  - GPUs         : `nvidia-smi --query-gpu=... --format=csv`

Bind address/port and upstreams are configurable via environment variables:
  STATUS_HOST (default 0.0.0.0)      STATUS_PORT (default 9095)
  LLAMASWAP_URL (default http://127.0.0.1:9090)
  COMFYUI_URLS  (default "open=http://127.0.0.1:8188,secure=http://127.0.0.1:8189")
  STATUS_CACHE_SECS (default 2)

Optional background workers:
  OWUI_API_KEY         set to enable pushing a live status banner into Open WebUI
  FAST_KEEPER_ENABLED  (default true) re-warm `fast` whenever the P100 slot is empty
  FAST_KEEP_MODEL      (default fast)   FAST_KEEP_ALT (default fast-uncensored)
  FAST_KEEPER_INTERVAL (default 60s)
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATUS_PORT", "9095"))
LLAMASWAP_URL = os.environ.get("LLAMASWAP_URL", "http://127.0.0.1:9090").rstrip("/")
COMFYUI_URLS = os.environ.get(
    "COMFYUI_URLS",
    "open=http://127.0.0.1:8188,secure=http://127.0.0.1:8189",
)
CACHE_SECS = float(os.environ.get("STATUS_CACHE_SECS", "2"))
HTTP_TIMEOUT = float(os.environ.get("STATUS_HTTP_TIMEOUT", "2.5"))

# --- Optional: push a live status banner into Open WebUI ---------------------
# When OWUI_API_KEY is set, a background thread periodically writes a top-of-UI
# banner (visible on the blank new-chat screen, before the user types). Requires
# an Open WebUI admin API key (Settings > Account > API Keys). The key is read
# from the environment only — never store it in git.
OWUI_BANNER_URL = os.environ.get(
    "OWUI_BANNER_URL", "http://127.0.0.1:3000/api/v1/configs/banners"
).rstrip("/")
OWUI_CONFIG_EXPORT_URL = os.environ.get(
    "OWUI_CONFIG_EXPORT_URL", "http://127.0.0.1:3000/api/v1/configs/export"
).rstrip("/")
OWUI_API_KEY = os.environ.get("OWUI_API_KEY", "").strip()
OWUI_BANNER_INTERVAL = float(os.environ.get("OWUI_BANNER_INTERVAL", "30"))
OWUI_BANNER_ID = os.environ.get("OWUI_BANNER_ID", "server-status")
OWUI_BANNER_TYPE = os.environ.get("OWUI_BANNER_TYPE", "info")
OWUI_BANNER_DISMISSIBLE = os.environ.get("OWUI_BANNER_DISMISSIBLE", "false").lower() in (
    "1",
    "true",
    "yes",
)

# --- Optional: keep the P100 `fast` model always resident ---------------------
# The P100 (idx0) slot is `(fast | fast-uncensored)` — mutually exclusive. A
# llama-swap restart, or `fast-uncensored`'s ttl expiring after use, can leave the
# card empty until something requests `fast`. This keeper re-warms `fast` whenever
# the P100 slot is empty. It never evicts `fast-uncensored` (if that is loaded the
# user is actively using it), so it only fires when NEITHER model is resident.
FAST_KEEPER_ENABLED = os.environ.get("FAST_KEEPER_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
FAST_KEEP_MODEL = os.environ.get("FAST_KEEP_MODEL", "fast")
FAST_KEEP_ALT = os.environ.get("FAST_KEEP_ALT", "fast-uncensored")
FAST_KEEPER_INTERVAL = float(os.environ.get("FAST_KEEPER_INTERVAL", "60"))
FAST_KEEPER_TIMEOUT = float(os.environ.get("FAST_KEEPER_TIMEOUT", "120"))


def _get_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            if 200 <= r.status < 300:
                return json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    return None


def _http_status(url: str):
    """Return (status_code, json_or_none). status_code is None on connection error."""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            body = r.read().decode("utf-8")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def collect_models() -> dict:
    running = _get_json(f"{LLAMASWAP_URL}/running")
    if running is None:
        return {"reachable": False, "loaded": [], "available": 0}
    rows = running.get("running") or []
    loaded = []
    for m in rows:
        if isinstance(m, dict):
            loaded.append({"model": m.get("model", "?"), "state": m.get("state", "?")})
    catalog = _get_json(f"{LLAMASWAP_URL}/v1/models") or {}
    available = len(catalog.get("data") or []) if isinstance(catalog, dict) else 0
    return {"reachable": True, "loaded": loaded, "available": available}


def collect_comfyui() -> list:
    out = []
    for pair in COMFYUI_URLS.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        label, url = pair.split("=", 1)
        label = label.strip()
        url = url.strip().rstrip("/")
        code, q = _http_status(f"{url}/queue")
        if code in (401, 403):
            out.append({"label": label, "state": "locked"})
            continue
        if code is None or q is None:
            out.append({"label": label, "state": "unreachable"})
            continue
        running = len(q.get("queue_running") or [])
        pending = len(q.get("queue_pending") or [])
        out.append(
            {
                "label": label,
                "state": "busy" if (running or pending) else "idle",
                "running": running,
                "pending": pending,
            }
        )
    return out


_GPU_FIELDS = [
    "index",
    "name",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "power.draw",
    "temperature.gpu",
]


def collect_gpus() -> list:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=" + ",".join(_GPU_FIELDS),
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=HTTP_TIMEOUT,
            env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"},
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    def _num(v, cast):
        v = v.strip()
        if v in ("", "[N/A]", "N/A", "[Not Supported]"):
            return None
        try:
            return cast(float(v))
        except Exception:
            return None

    gpus = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(_GPU_FIELDS):
            continue
        gpus.append(
            {
                "index": _num(parts[0], int),
                "name": parts[1],
                "util": _num(parts[2], int),
                "mem_used": _num(parts[3], int),
                "mem_total": _num(parts[4], int),
                "power": _num(parts[5], int),
                "temp": _num(parts[6], int),
            }
        )
    return gpus


def _summary(models: dict, comfyui: list, gpus: list) -> str:
    if not models.get("reachable"):
        mtxt = "llama-swap unreachable"
    elif models["loaded"]:
        mtxt = "loaded: " + ", ".join(m["model"] for m in models["loaded"])
    else:
        mtxt = "no models loaded (idle)"
    parts = [mtxt]
    busy = [c["label"] for c in comfyui if c.get("state") == "busy"]
    if busy:
        parts.append("ComfyUI busy: " + ", ".join(busy))
    if gpus:
        parts.append(f"{len(gpus)} GPUs")
    return "  ·  ".join(parts)


def build_status() -> dict:
    models = collect_models()
    comfyui = collect_comfyui()
    gpus = collect_gpus()
    return {
        "timestamp": int(time.time()),
        "summary": _summary(models, comfyui, gpus),
        "models": models,
        "comfyui": comfyui,
        "gpus": gpus,
    }


_cache = {"at": 0.0, "data": None}


def cached_status() -> dict:
    now = time.time()
    if _cache["data"] is None or (now - _cache["at"]) > CACHE_SECS:
        _cache["data"] = build_status()
        _cache["at"] = now
    return _cache["data"]


# --- Open WebUI banner pusher -----------------------------------------------

def banner_text(status: dict) -> str:
    """One-line banner string for the OWUI top bar."""
    models = status.get("models") or {}
    if not models.get("reachable", True):
        mtxt = "Models: llama-swap unreachable"
    else:
        loaded = models.get("loaded") or []
        if loaded:
            mtxt = "Models: " + ", ".join(m.get("model", "?") for m in loaded)
            if models.get("available"):
                mtxt += f" ({models['available']} avail)"
        else:
            mtxt = "Models: idle"
    parts = [mtxt]

    gpus = status.get("gpus") or []
    if gpus:
        g = []
        for gpu in gpus:
            used = gpu.get("mem_used")
            total = gpu.get("mem_total")
            mem = ""
            if used is not None and total:
                mem = f" {used/1024:.0f}/{total/1024:.0f}GB"
            util = gpu.get("util")
            u = f"{util}%" if util is not None else "–"
            g.append(f"GPU{gpu.get('index', '?')} {u}{mem}")
        parts.append("  ".join(g))

    comfy = status.get("comfyui") or []
    busy = [c["label"] for c in comfy if c.get("state") == "busy"]
    if busy:
        parts.append("ComfyUI busy: " + ", ".join(busy))

    return "🖥️  " + "  |  ".join(parts)


def _owui_get(url: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {OWUI_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            if 200 <= r.status < 300:
                return json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    return None


def _owui_existing_banners() -> list:
    """Fetch current banners so we preserve any not owned by this service."""
    data = _owui_get(OWUI_CONFIG_EXPORT_URL)
    if not isinstance(data, dict):
        return []
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    banners = ui.get("banners") if isinstance(ui, dict) else None
    if banners is None:
        banners = data.get("banners")
    if not isinstance(banners, list):
        return []
    return [b for b in banners if isinstance(b, dict) and b.get("id") != OWUI_BANNER_ID]


def _owui_push_banner() -> bool:
    status = cached_status()
    banner = {
        "id": OWUI_BANNER_ID,
        "type": OWUI_BANNER_TYPE,
        "title": "",
        "content": banner_text(status),
        "dismissible": OWUI_BANNER_DISMISSIBLE,
        "timestamp": int(time.time()),
    }
    payload = {"banners": _owui_existing_banners() + [banner]}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OWUI_BANNER_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {OWUI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[banner] push failed: {e}")
        return False


def _banner_loop():
    print(f"[banner] pushing OWUI status banner every {OWUI_BANNER_INTERVAL:.0f}s")
    while True:
        _owui_push_banner()
        time.sleep(OWUI_BANNER_INTERVAL)


def _warm_model(model: str) -> bool:
    """Send a tiny request so llama-swap loads `model`. Returns True on success."""
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    ).encode()
    req = urllib.request.Request(
        f"{LLAMASWAP_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=FAST_KEEPER_TIMEOUT) as resp:
            resp.read()
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort warmup
        print(f"[keeper] warmup of {model!r} failed: {exc}")
        return False


def _fast_keeper_loop():
    print(
        f"[keeper] keeping {FAST_KEEP_MODEL!r} resident on the P100 "
        f"(checked every {FAST_KEEPER_INTERVAL:.0f}s; yields to {FAST_KEEP_ALT!r})"
    )
    while True:
        try:
            running = _get_json(f"{LLAMASWAP_URL}/running")
            if running is not None:
                loaded = {
                    m.get("model")
                    for m in (running.get("running") or [])
                    if isinstance(m, dict)
                }
                # Only warm when the P100 slot is empty (neither variant loaded),
                # so we never evict fast-uncensored while it is in use.
                if FAST_KEEP_MODEL not in loaded and FAST_KEEP_ALT not in loaded:
                    print(f"[keeper] P100 slot empty — warming {FAST_KEEP_MODEL!r}")
                    _warm_model(FAST_KEEP_MODEL)
        except Exception as exc:  # noqa: BLE001 - keeper must never crash the service
            print(f"[keeper] loop error: {exc}")
        time.sleep(FAST_KEEPER_INTERVAL)



_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Server Status</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background:#0f1115; color:#e6e6e6; }
  header { padding: 16px 20px; background:#151922; border-bottom:1px solid #232a36; }
  h1 { font-size: 18px; margin:0; }
  .sub { color:#8b95a7; font-size: 12px; margin-top:4px; }
  main { padding: 20px; display:grid; gap:20px; max-width: 900px; }
  section { background:#151922; border:1px solid #232a36; border-radius:10px; padding:14px 16px; }
  h2 { font-size: 13px; text-transform:uppercase; letter-spacing:.05em; color:#8b95a7; margin:0 0 10px; }
  table { width:100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #202634; }
  th { color:#8b95a7; font-weight:600; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; }
  .idle { background:#1e2a1e; color:#7fce7f; }
  .busy { background:#2a2410; color:#e6c04b; }
  .bad  { background:#2a1616; color:#e07f7f; }
  .bar { background:#202634; border-radius:4px; height:8px; overflow:hidden; width:120px; display:inline-block; vertical-align:middle; }
  .bar > i { display:block; height:100%; background:#4b8ce0; }
</style></head>
<body>
<header><h1>AI Server Status</h1><div class="sub" id="sub">loading…</div></header>
<main>
  <section><h2>Models (llama-swap)</h2><div id="models">…</div></section>
  <section><h2>GPUs</h2><div id="gpus">…</div></section>
  <section><h2>ComfyUI</h2><div id="comfyui">…</div></section>
</main>
<script>
function pill(state){const c=state==='idle'?'idle':(state==='busy'?'busy':'bad');return `<span class="pill ${c}">${state}</span>`;}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function refresh(){
  try{
    const r = await fetch('status.json', {cache:'no-store'}); const d = await r.json();
    document.getElementById('sub').textContent = d.summary + '  ·  updated ' + new Date(d.timestamp*1000).toLocaleTimeString();
    // models
    let m = d.models;
    if(!m.reachable){ document.getElementById('models').innerHTML = pill('unreachable'); }
    else if(!m.loaded.length){ document.getElementById('models').innerHTML = pill('idle') + ` <span class="sub">${m.available} available</span>`; }
    else {
      document.getElementById('models').innerHTML =
        '<table><tr><th>Model</th><th>State</th></tr>' +
        m.loaded.map(x=>`<tr><td>${esc(x.model)}</td><td>${pill(x.state==='ready'?'idle':'busy')} ${esc(x.state)}</td></tr>`).join('') +
        `</table><div class="sub" style="margin-top:8px">${m.available} available</div>`;
    }
    // gpus
    document.getElementById('gpus').innerHTML = d.gpus.length ?
      '<table><tr><th>#</th><th>Name</th><th>Util</th><th>VRAM</th><th>Power</th><th>Temp</th></tr>' +
      d.gpus.map(g=>{
        const memPct = (g.mem_used!=null&&g.mem_total)? Math.round(100*g.mem_used/g.mem_total):0;
        return `<tr><td>${g.index}</td><td>${esc(g.name)}</td>`+
        `<td>${g.util!=null?g.util+'%':'–'}</td>`+
        `<td><span class="bar"><i style="width:${memPct}%"></i></span> ${g.mem_used??'–'}/${g.mem_total??'–'} MB</td>`+
        `<td>${g.power!=null?g.power+' W':'–'}</td>`+
        `<td>${g.temp!=null?g.temp+'°C':'–'}</td></tr>`;
      }).join('') + '</table>'
      : pill('unavailable');
    // comfyui
    document.getElementById('comfyui').innerHTML = d.comfyui.length ?
      d.comfyui.map(c=>`<div style="margin:4px 0">${esc(c.label)}: ${pill(c.state)}` +
        (c.state==='busy'?` <span class="sub">${c.running} running, ${c.pending} queued</span>`:'') + `</div>`).join('')
      : pill('none');
  }catch(e){ document.getElementById('sub').textContent = 'status service error: ' + e; }
}
refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/status.json", "/status"):
            body = json.dumps(cached_status()).encode("utf-8")
            self._send(200, body, "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        elif path == "/":
            self._send(200, _HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):  # silence per-request stderr logging
        pass


def main():
    if OWUI_API_KEY:
        threading.Thread(target=_banner_loop, daemon=True).start()
    else:
        print("[banner] OWUI_API_KEY not set — OWUI banner push disabled")
    if FAST_KEEPER_ENABLED:
        threading.Thread(target=_fast_keeper_loop, daemon=True).start()
    else:
        print("[keeper] FAST_KEEPER_ENABLED=false — fast keeper disabled")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ai-server status service listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

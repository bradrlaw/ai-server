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
  - model activity: each loaded model's own llama-server /slots (live prefill/decode
                    state + prompt size + cache reuse) and /metrics (prompt/decode t/s),
                    polled on a background thread and cached
  - ComfyUI      : http://127.0.0.1:8188/queue     (open, no auth)
                   http://127.0.0.1:8189/queue     (secure, login-gated -> "locked")
  - GPUs         : `nvidia-smi --query-gpu=... --format=csv`

Bind address/port and upstreams are configurable via environment variables:
  STATUS_HOST (default 0.0.0.0)      STATUS_PORT (default 9095)
  LLAMASWAP_URL (default http://127.0.0.1:9090)
  COMFYUI_URLS  (default "open=http://127.0.0.1:8188,secure=http://127.0.0.1:8189")
  STATUS_DISK_PATHS (default "/", comma-separated filesystems to report)
  STATUS_CACHE_SECS (default 2)

Optional background workers:
  OWUI_API_KEY         set to enable pushing a live status banner into Open WebUI
  FAST_KEEPER_ENABLED  (default true) re-warm `fast` whenever the P100 slot is empty
  FAST_KEEP_MODEL      (default fast)   FAST_KEEP_ALT (default fast-uncensored)
  FAST_KEEPER_INTERVAL (default 60s)
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import datetime as dt
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("STATUS_PORT", "9095"))
LLAMASWAP_URL = os.environ.get("LLAMASWAP_URL", "http://127.0.0.1:9090").rstrip("/")
# Active llama-swap config the mode switcher (scripts/llama-swap-mode.py) renders;
# read for the current mode marker + per-model --parallel / --ctx-size.
LLAMASWAP_CONFIG = os.environ.get("LLAMASWAP_CONFIG", "/srv/ai/config/llama-swap.yaml")
# Committed benchmark chart (docs/img/…) served at /parallel-sweep.png and shown
# in the dashboard's Benchmarks section. Empty/missing → section hidden.
BENCH_CHART = os.environ.get(
    "BENCH_CHART", "/srv/ai/docs/img/parallel-sweep-20260721.png")
BENCH_DOC_URL = os.environ.get(
    "BENCH_DOC_URL",
    "https://github.com/bradrlaw/ai-server/blob/dev/docs/benchmarking.md")
# Comma-separated filesystem paths to report disk usage for (one row each).
STATUS_DISK_PATHS = [
    p.strip() for p in os.environ.get("STATUS_DISK_PATHS", "/").split(",") if p.strip()
]
COMFYUI_URLS = os.environ.get(
    "COMFYUI_URLS",
    "open=http://127.0.0.1:8188,secure=http://127.0.0.1:8189",
)
# App-tier "Services" panel: health-check a list of endpoints and show up/down +
# a click-through link. Format: "name=health_url=link_port" items, comma-separated
# (link_port optional; blank = no link). Health is checked from the host
# (127.0.0.1); the link is built client-side from the browser's own hostname, so
# LAN and Tailscale addresses both resolve. Any HTTP response (incl. 401/403 for
# auth-gated UIs) counts as "up"; only a connection failure is "down".
SERVICES = os.environ.get(
    "STATUS_SERVICES",
    "Open WebUI=http://127.0.0.1:3000/health=3000,"
    "LiteLLM=http://127.0.0.1:4000/health/liveliness=,"
    "mcpo=http://127.0.0.1:8000/docs=8000,"
    "Filebrowser=http://127.0.0.1:8083/health=8083,"
    "OpenClaw=http://127.0.0.1:18789/healthz=18789,"
    "Hermes dashboard=http://127.0.0.1:9119/=9119,"
    "Hermes API=http://127.0.0.1:8642/health=8642",
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

# --- Quiet hours (deep-idle window) -----------------------------------------
# During the window the daily models are unloaded and ComfyUI is stopped so the
# V100s can drop out of P0 to true cold idle. Any client LLM request still wakes
# llama-swap on demand; when that happens we restart ComfyUI so the box is fully
# ready, then re-idle after it has been quiet for QUIET_ACTIVITY_GRACE seconds.
QUIET_HOURS_ENABLED = os.environ.get("QUIET_HOURS_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
QUIET_HOURS_START = os.environ.get("QUIET_HOURS_START", "02:00")
QUIET_HOURS_END = os.environ.get("QUIET_HOURS_END", "09:00")
# Timezone the window is evaluated in (e.g. America/New_York). Empty = system local
# time. Set this if the machine's clock runs in UTC but you want a wall-clock window.
QUIET_TZ = os.environ.get("QUIET_TZ", "").strip()
QUIET_CHECK_INTERVAL = float(os.environ.get("QUIET_CHECK_INTERVAL", "30"))
QUIET_ACTIVITY_GRACE = float(os.environ.get("QUIET_ACTIVITY_GRACE", "600"))
QUIET_UNLOAD_MODELS = os.environ.get("QUIET_UNLOAD_MODELS", "true").lower() in (
    "1",
    "true",
    "yes",
)
QUIET_STOP_COMFYUI = os.environ.get("QUIET_STOP_COMFYUI", "true").lower() in (
    "1",
    "true",
    "yes",
)
QUIET_COMFYUI_UNITS = [
    u.strip()
    for u in os.environ.get(
        "QUIET_COMFYUI_UNITS", "comfyui-open,comfyui-secure"
    ).split(",")
    if u.strip()
]
# Command prefix used to control the ComfyUI units (needs a scoped sudoers rule).
QUIET_SYSTEMCTL = os.environ.get("QUIET_SYSTEMCTL", "sudo systemctl").split()
# Models re-warmed when the window ends (fast is handled by the keeper).
QUIET_WARM_ON_EXIT = [
    m.strip()
    for m in os.environ.get("QUIET_WARM_ON_EXIT", "coding,chat").split(",")
    if m.strip()
]
# While "woken", re-idle once GPU SM utilization stays below this %% for the grace
# period. Loaded-but-idle models sit at ~0%%, so this distinguishes "in use" from
# "just resident" (coding/chat have no ttl and never self-unload).
QUIET_ACTIVE_SM_PCT = float(os.environ.get("QUIET_ACTIVE_SM_PCT", "5"))

# --- History (in-memory time series for the dashboard sparklines) -------------
# A background thread snapshots per-GPU util/power/temp/VRAM and host CPU/RAM %
# into a fixed-size ring buffer, served at /history.json for inline-SVG graphs.
# Purely in-memory (no deps, no disk) — cleared on restart. Defaults: sample
# every 15s, keep 240 points => ~1h of history.
HISTORY_ENABLED = os.environ.get("HISTORY_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
HISTORY_INTERVAL = float(os.environ.get("HISTORY_INTERVAL", "15"))
HISTORY_POINTS = int(os.environ.get("HISTORY_POINTS", "240"))

# Set while quiet hours has the box in deep idle — the fast keeper honours this
# and stops re-warming so it doesn't fight the quiet-hours loop.
_QUIET_SUPPRESS_KEEPER = threading.Event()
# Reported in status.json so the dashboard/banner can show the current mode.
_power_state = {"mode": "active", "since": time.time(), "detail": ""}


def _set_power_state(mode: str, detail: str = "") -> None:
    if _power_state["mode"] != mode:
        _power_state["since"] = time.time()
    _power_state["mode"] = mode
    _power_state["detail"] = detail


def _get_json(url: str, timeout: float | None = None):
    try:
        with urllib.request.urlopen(url, timeout=timeout or HTTP_TIMEOUT) as r:
            if 200 <= r.status < 300:
                return json.loads(r.read().decode("utf-8"))
    except Exception:
        pass
    return None


def _get_text(url: str, timeout: float | None = None):
    try:
        with urllib.request.urlopen(url, timeout=timeout or HTTP_TIMEOUT) as r:
            if 200 <= r.status < 300:
                return r.read().decode("utf-8")
    except Exception:
        pass
    return None


def _parse_prom_metrics(txt: str) -> dict:
    """Parse a llama.cpp Prometheus /metrics body into {metric: float}."""
    out: dict[str, float] = {}
    if not txt:
        return out
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                val = float(parts[1])
            except ValueError:
                continue
            # llama.cpp can emit inf/nan gauges (e.g. before the first request);
            # those are invalid JSON and would break the page's JSON.parse.
            if math.isfinite(val):
                out[parts[0]] = val
    return out


# Activity probes hit the model's own llama-server, whose /slots and /metrics are
# served on its busy main loop — latency swings from ~0.2s to several seconds while
# it is actively inferring. So we probe them on a BACKGROUND thread (generous
# timeout) and cache the result; the status page reads the cache instantly and
# never blocks on a slow model.
ACT_TIMEOUT = max(HTTP_TIMEOUT, 6.0)
ACT_POLL_SECS = float(os.environ.get("STATUS_ACT_POLL_SECS", "2"))
ACT_STALE_SECS = float(os.environ.get("STATUS_ACT_STALE_SECS", "12"))
_activity_cache: dict[str, dict] = {}
_activity_lock = threading.Lock()


def collect_model_activity(proxy_url: str) -> dict | None:
    """Per-model live inference state from the model's own llama-server:
    active slots (prefill/decode + progress) and prompt/decode throughput."""
    base = proxy_url.rstrip("/")
    slots = _get_json(f"{base}/slots", timeout=ACT_TIMEOUT)
    metrics_txt = _get_text(f"{base}/metrics", timeout=ACT_TIMEOUT)
    metrics = _parse_prom_metrics(metrics_txt)
    slots_ok = isinstance(slots, list)
    metrics_ok = metrics_txt is not None
    # Both probes hit llama-server's busy main loop and can time out mid-inference.
    # If neither responded we have nothing new — signal the caller to keep the last
    # known sample rather than flip the page to a false "idle".
    if not slots_ok and not metrics_ok:
        return None

    active = []
    if slots_ok:
        for s in slots:
            if not isinstance(s, dict) or not s.get("is_processing"):
                continue
            n_prompt = int(s.get("n_prompt_tokens") or 0)
            cache = int(s.get("n_prompt_tokens_cache") or 0)
            processed = int(s.get("n_prompt_tokens_processed") or 0)
            fresh = max(0, n_prompt - cache)
            # Still working through fresh prompt tokens => prefill; else generating.
            phase = "prefill" if processed < fresh else "decode"
            active.append(
                {
                    "id": s.get("id"),
                    "phase": phase,
                    "n_prompt": n_prompt,
                    "cache": cache,
                    "processed": processed,
                    "fresh": fresh,
                    "n_ctx": int(s.get("n_ctx") or 0),
                }
            )

    processing = int(metrics.get("llamacpp:requests_processing", 0))
    deferred = int(metrics.get("llamacpp:requests_deferred", 0))
    # requests_processing (from /metrics) is the authoritative busy signal. If it
    # says work is in flight but /slots timed out (common during heavy prefill),
    # still report busy with a placeholder so the page doesn't show a false idle.
    busy = processing > 0 or bool(active)
    if busy and not active:
        active = [{"id": None, "phase": "working"}]

    return {
        "active": active,
        "busy": busy,
        "slots_ok": slots_ok,
        "prompt_tps": metrics.get("llamacpp:prompt_tokens_seconds"),
        "decode_tps": metrics.get("llamacpp:predicted_tokens_seconds"),
        "processing": processing,
        "deferred": deferred,
    }


def _cached_activity(proxy: str) -> dict | None:
    """Latest cached activity for a model proxy, or None if missing/stale."""
    with _activity_lock:
        c = _activity_cache.get(proxy)
    if c and c.get("data") is not None and (time.time() - c["at"]) <= ACT_STALE_SECS:
        return c["data"]
    return None


def _activity_loop() -> None:
    """Poll each loaded model's llama-server for live slot/throughput activity and
    cache it, so the (2s-cached) status page never blocks on a slow model probe."""
    while True:
        try:
            running = _get_json(f"{LLAMASWAP_URL}/running")
            proxies = set()
            rows = running.get("running") if isinstance(running, dict) else None
            for m in rows or []:
                if isinstance(m, dict) and m.get("proxy"):
                    proxies.add(m["proxy"])
            for proxy in proxies:
                data = collect_model_activity(proxy)
                # Keep the last good sample when both probes time out (data is None)
                # so a momentary stall doesn't flash the page to a false "idle".
                if data is not None:
                    with _activity_lock:
                        _activity_cache[proxy] = {"at": time.time(), "data": data}
            with _activity_lock:
                for k in list(_activity_cache):
                    if k not in proxies and (time.time() - _activity_cache[k]["at"]) > ACT_STALE_SECS:
                        del _activity_cache[k]
        except Exception:
            pass
        time.sleep(ACT_POLL_SECS)


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


def collect_mode() -> dict:
    """Read the active llama-swap mode marker + per-model --parallel / --ctx-size.

    Parses the rendered active config (config/llama-swap.yaml) written by the mode
    switcher. Returns {"mode": <name>, "models": {name: {parallel, ctx,
    ctx_per_slot}}}. Best-effort — returns a mostly-empty dict on any error.
    """
    out = {"mode": "unknown", "models": {}}
    try:
        with open(LLAMASWAP_CONFIG) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    key_re = re.compile(r'^  "([a-z0-9-]+)":\s*$')
    for line in lines:
        mm = re.match(r"^#\s*ACTIVE-MODE:\s*([A-Za-z0-9_-]+)", line)
        if mm:
            out["mode"] = mm.group(1)
            break
        if line.strip() and not line.startswith("#"):
            break
    i, n = 0, len(lines)
    while i < n:
        m = key_re.match(lines[i])
        if m:
            name = m.group(1)
            j = i + 1
            block = []
            while j < n and not key_re.match(lines[j]):
                block.append(lines[j])
                j += 1
            text = "\n".join(block)
            info = {}
            if (pm := re.search(r"--parallel\s+(\d+)", text)):
                info["parallel"] = int(pm.group(1))
            if (cm := re.search(r"--ctx-size\s+(\d+)", text)):
                info["ctx"] = int(cm.group(1))
            if info.get("ctx"):
                info["ctx_per_slot"] = info["ctx"] // (info.get("parallel", 1) or 1)
            if info:
                out["models"][name] = info
            i = j
        else:
            i += 1
    return out


def collect_models() -> dict:
    running = _get_json(f"{LLAMASWAP_URL}/running")
    cfg = collect_mode()
    if running is None:
        return {"reachable": False, "loaded": [], "available": 0,
                "mode": cfg["mode"], "config": cfg["models"]}
    rows = running.get("running") or []
    loaded = []
    for m in rows:
        if isinstance(m, dict):
            name = m.get("model", "?")
            entry = {"model": name, "state": m.get("state", "?")}
            mc = cfg["models"].get(name)
            if mc:
                entry["parallel"] = mc.get("parallel")
                entry["ctx"] = mc.get("ctx")
                entry["ctx_per_slot"] = mc.get("ctx_per_slot")
            proxy = m.get("proxy")
            if proxy:
                act = _cached_activity(proxy)
                if act is not None:
                    entry["activity"] = act
            loaded.append(entry)
    catalog = _get_json(f"{LLAMASWAP_URL}/v1/models") or {}
    available = len(catalog.get("data") or []) if isinstance(catalog, dict) else 0
    return {"reachable": True, "loaded": loaded, "available": available,
            "mode": cfg["mode"], "config": cfg["models"]}


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


def collect_services() -> list:
    """Health-check the configured app-tier services (SERVICES env). Each entry:
    {name, up, code, port}. up=True for any HTTP response (auth-gated UIs return
    401/403 but are still 'up'); up=False only on connection failure."""
    out = []
    for item in SERVICES.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        parts = item.split("=")
        name = parts[0].strip()
        health = parts[1].strip() if len(parts) > 1 else ""
        port = parts[2].strip() if len(parts) > 2 else ""
        if not name or not health:
            continue
        code, _ = _http_status(health)
        out.append(
            {
                "name": name,
                "up": code is not None,
                "code": code,
                "port": int(port) if port.isdigit() else None,
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


_cpu_prev = {"total": None, "idle": None}


def _cpu_percent():
    """Instantaneous CPU %% computed from /proc/stat deltas across calls."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    vals = [int(x) for x in line.split()[1:]]
                    break
            else:
                return None
    except Exception:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals)
    prev_total, prev_idle = _cpu_prev["total"], _cpu_prev["idle"]
    _cpu_prev["total"], _cpu_prev["idle"] = total, idle
    if prev_total is None:
        return None
    dt = total - prev_total
    if dt <= 0:
        return None
    return round(100.0 * (dt - (idle - prev_idle)) / dt, 1)


def _mem_info():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])  # kB
    except Exception:
        return None
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    if total is None or avail is None:
        return None
    used = total - avail
    return {
        "total_mb": round(total / 1024),
        "used_mb": round(used / 1024),
        "used_pct": round(100.0 * used / total, 1) if total else None,
    }


def _disk_info(paths):
    out = []
    for p in paths:
        try:
            st = os.statvfs(p)
        except Exception:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        out.append(
            {
                "path": p,
                "total_gb": round(total / 1e9, 1),
                "used_gb": round(used / 1e9, 1),
                "used_pct": round(100.0 * used / total, 1) if total else None,
            }
        )
    return out


def collect_host() -> dict:
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        load = None
    return {
        "cpu_pct": _cpu_percent(),
        "cpus": os.cpu_count(),
        "load": load,
        "mem": _mem_info(),
        "disk": _disk_info(STATUS_DISK_PATHS),
    }


def _summary(models: dict, comfyui: list, gpus: list, host: dict | None = None) -> str:
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
    if host:
        hp = []
        if host.get("cpu_pct") is not None:
            hp.append(f"CPU {host['cpu_pct']:.0f}%")
        mem = host.get("mem")
        if mem and mem.get("used_pct") is not None:
            hp.append(f"RAM {mem['used_pct']:.0f}%")
        disk = host.get("disk") or []
        if disk and disk[0].get("used_pct") is not None:
            hp.append(f"disk {disk[0]['used_pct']:.0f}%")
        if hp:
            parts.append(" ".join(hp))
    return "  ·  ".join(parts)


def build_status() -> dict:
    models = collect_models()
    comfyui = collect_comfyui()
    gpus = collect_gpus()
    host = collect_host()
    return {
        "timestamp": int(time.time()),
        "summary": _summary(models, comfyui, gpus, host),
        "models": models,
        "comfyui": comfyui,
        "services": collect_services(),
        "gpus": gpus,
        "host": host,
        "power_mode": _power_state["mode"],
    }


_cache = {"at": 0.0, "data": None}


def cached_status() -> dict:
    now = time.time()
    if _cache["data"] is None or (now - _cache["at"]) > CACHE_SECS:
        _cache["data"] = build_status()
        _cache["at"] = now
    return _cache["data"]


# --- History ring buffer -----------------------------------------------------
# Compact per-sample record: {"t", "cpu", "mem", "g":[{"i","n","u","p","tC","m"}]}.
# Reads cached_status() so it shares the single CPU%-delta state (no double
# counting) and one nvidia-smi cadence with the page.
_history: "deque[dict]" = deque(maxlen=HISTORY_POINTS)
_history_lock = threading.Lock()


def _compact_sample(s: dict) -> dict:
    host = s.get("host") or {}
    mem = host.get("mem") or {}
    g = []
    for gpu in s.get("gpus") or []:
        mu, mt = gpu.get("mem_used"), gpu.get("mem_total")
        g.append(
            {
                "i": gpu.get("index"),
                "n": gpu.get("name"),
                "u": gpu.get("util"),
                "p": gpu.get("power"),
                "tC": gpu.get("temp"),
                "m": round(100.0 * mu / mt) if (mu is not None and mt) else None,
            }
        )
    return {
        "t": s.get("timestamp"),
        "cpu": host.get("cpu_pct"),
        "mem": mem.get("used_pct"),
        "g": g,
    }


def _history_loop() -> None:
    while True:
        try:
            rec = _compact_sample(cached_status())
            with _history_lock:
                _history.append(rec)
        except Exception:
            pass
        time.sleep(HISTORY_INTERVAL)


def build_history() -> dict:
    with _history_lock:
        snap = list(_history)
    names: dict = {}
    for rec in snap:
        for gpu in rec.get("g", []):
            i = gpu.get("i")
            if i is not None and i not in names:
                names[i] = gpu.get("n")
    idxs = sorted(names)
    gpus = []
    for i in idxs:
        util, power, temp, memp = [], [], [], []
        for rec in snap:
            gg = next((x for x in rec.get("g", []) if x.get("i") == i), None)
            util.append(gg.get("u") if gg else None)
            power.append(gg.get("p") if gg else None)
            temp.append(gg.get("tC") if gg else None)
            memp.append(gg.get("m") if gg else None)
        gpus.append(
            {"index": i, "name": names[i], "util": util, "power": power,
             "temp": temp, "mem": memp}
        )
    return {
        "interval": HISTORY_INTERVAL,
        "points": HISTORY_POINTS,
        "count": len(snap),
        "t": [rec.get("t") for rec in snap],
        "cpu": [rec.get("cpu") for rec in snap],
        "mem": [rec.get("mem") for rec in snap],
        "gpus": gpus,
    }


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

    host = status.get("host") or {}
    hp = []
    if host.get("cpu_pct") is not None:
        hp.append(f"CPU {host['cpu_pct']:.0f}%")
    mem = host.get("mem")
    if mem and mem.get("used_pct") is not None:
        hp.append(f"RAM {mem['used_mb'] / 1024:.0f}/{mem['total_mb'] / 1024:.0f}GB")
    disk = host.get("disk") or []
    if disk and disk[0].get("used_pct") is not None:
        hp.append(f"disk {disk[0]['used_pct']:.0f}%")
    if hp:
        parts.append(" ".join(hp))

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
                if (
                    not _QUIET_SUPPRESS_KEEPER.is_set()
                    and FAST_KEEP_MODEL not in loaded
                    and FAST_KEEP_ALT not in loaded
                ):
                    print(f"[keeper] P100 slot empty — warming {FAST_KEEP_MODEL!r}")
                    _warm_model(FAST_KEEP_MODEL)
        except Exception as exc:  # noqa: BLE001 - keeper must never crash the service
            print(f"[keeper] loop error: {exc}")
        time.sleep(FAST_KEEPER_INTERVAL)


# --- Quiet hours (deep-idle window) -----------------------------------------

def _parse_hhmm(value: str) -> dt.time:
    hh, _, mm = value.strip().partition(":")
    return dt.time(int(hh), int(mm or 0))


def _quiet_now() -> dt.time:
    """Current wall-clock time in QUIET_TZ (falls back to system local time)."""
    if QUIET_TZ:
        try:
            from zoneinfo import ZoneInfo

            return dt.datetime.now(ZoneInfo(QUIET_TZ)).time()
        except Exception as exc:  # noqa: BLE001 - bad tz name / missing tzdata
            print(f"[quiet] invalid QUIET_TZ {QUIET_TZ!r} ({exc}); using system local")
    return dt.datetime.now().time()


def _in_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    """True if `now` is within [start, end). Handles windows that wrap midnight."""
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight


def _loaded_models() -> set:
    running = _get_json(f"{LLAMASWAP_URL}/running")
    if not running:
        return set()
    return {
        m.get("model")
        for m in (running.get("running") or [])
        if isinstance(m, dict)
    }


def _max_gpu_util() -> int:
    """Highest SM utilization %% across all GPUs (0 if unavailable). Used to detect
    real inference activity — a loaded-but-idle model sits at ~0%%."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=HTTP_TIMEOUT,
            env={**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"},
        )
        if proc.returncode != 0:
            return 0
        vals = [int(x.strip()) for x in proc.stdout.split() if x.strip().isdigit()]
        return max(vals) if vals else 0
    except Exception:  # noqa: BLE001 - best effort
        return 0


def _unload_all_models() -> None:
    try:
        req = urllib.request.Request(
            f"{LLAMASWAP_URL}/api/models/unload",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001 - best effort
        print(f"[quiet] unload-all failed: {exc}")


def _comfyui(action: str) -> None:
    """Start or stop the ComfyUI units via the configured systemctl prefix."""
    if not QUIET_STOP_COMFYUI or not QUIET_COMFYUI_UNITS:
        return
    cmd = [*QUIET_SYSTEMCTL, action, *QUIET_COMFYUI_UNITS]
    try:
        subprocess.run(cmd, check=False, timeout=60)
    except Exception as exc:  # noqa: BLE001 - best effort
        print(f"[quiet] '{' '.join(cmd)}' failed: {exc}")


def _enter_deep_idle() -> None:
    print("[quiet] entering deep idle — unloading models + stopping ComfyUI")
    _QUIET_SUPPRESS_KEEPER.set()
    if QUIET_UNLOAD_MODELS:
        _unload_all_models()
    _comfyui("stop")
    _set_power_state("deep-idle", "quiet hours")


def _wake_for_activity() -> None:
    print("[quiet] client activity — restarting ComfyUI (staying in window)")
    _comfyui("start")
    _set_power_state("woken", "quiet hours (activity)")


def _exit_window() -> None:
    print("[quiet] window ended — restoring active state")
    _QUIET_SUPPRESS_KEEPER.clear()
    _comfyui("start")
    for m in QUIET_WARM_ON_EXIT:
        _warm_model(m)
    _set_power_state("active", "")


def _quiet_hours_loop():
    start = _parse_hhmm(QUIET_HOURS_START)
    end = _parse_hhmm(QUIET_HOURS_END)
    print(
        f"[quiet] deep-idle window {QUIET_HOURS_START}–{QUIET_HOURS_END} "
        f"({QUIET_TZ or 'system local'}); activity grace {QUIET_ACTIVITY_GRACE:.0f}s"
    )
    # phase: "active" (outside window) | "idle" (in window, deep idle)
    #        | "woken" (in window, ComfyUI up because a client is active)
    phase = "active"
    last_activity = 0.0
    while True:
        try:
            in_window = _in_window(_quiet_now(), start, end)
            models_loaded = bool(_loaded_models())
            # Real inference (LLM tokens or a ComfyUI render) spikes SM utilization;
            # a loaded-but-idle model sits near 0%%. Use that to time the re-idle so
            # the no-ttl models (coding/chat) don't pin the box "woken" all window.
            busy = _max_gpu_util() >= QUIET_ACTIVE_SM_PCT
            if busy:
                last_activity = time.time()

            if not in_window:
                if phase != "active":
                    _exit_window()
                    phase = "active"
            elif phase == "active":
                _enter_deep_idle()
                phase = "idle"
            elif phase == "idle":
                # A client loaded a model on demand → wake so the box is fully ready.
                if models_loaded:
                    _wake_for_activity()
                    last_activity = time.time()
                    phase = "woken"
            elif phase == "woken":
                if (time.time() - last_activity) > QUIET_ACTIVITY_GRACE:
                    _enter_deep_idle()
                    phase = "idle"
        except Exception as exc:  # noqa: BLE001 - loop must never crash the service
            print(f"[quiet] loop error: {exc}")
        time.sleep(QUIET_CHECK_INTERVAL)



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
  .bar > i.busy { background:#e6c04b; }
  .bar > i.bad { background:#e07f7f; }
  .spark { display:inline-block; vertical-align:middle; background:#0f1115; border:1px solid #202634; border-radius:3px; }
  .spark path { vector-effect: non-scaling-stroke; }
  .cur { display:inline-block; min-width:52px; text-align:right; font-variant-numeric:tabular-nums; margin-left:6px; color:#cdd6e4; }
  td.g { white-space:nowrap; }
  .hsub { color:#8b95a7; font-size:11px; }
</style></head>
<body>
<header><h1>AI Server Status</h1><div class="sub" id="sub">loading…</div></header>
<main>
  <section><h2>Models (llama-swap) <span class="hsub" id="modebadge"></span></h2><div id="models">…</div></section>
  <section><h2>GPUs</h2><div id="gpus">…</div></section>
  <section><h2>History <span class="hsub" id="histspan"></span></h2><div id="history">…</div></section>
  <section><h2>Host (CPU / RAM / Disk)</h2><div id="host">…</div></section>
  <section><h2>ComfyUI</h2><div id="comfyui">…</div></section>
  <section><h2>Services</h2><div id="services">…</div></section>
  <section id="benchsec"><h2>Benchmarks <span class="hsub">· <a id="benchlink" href="__BENCH_DOC_URL__" target="_blank" style="color:#8b95a7">docs/benchmarking.md</a></span></h2>
    <div class="sub" style="margin-bottom:8px">llama-swap <code>--parallel</code> throughput sweep — peak aggregate tok/s per model (higher = more concurrent throughput; raising <code>--parallel</code> divides per-request context).</div>
    <a id="benchimglink" href="parallel-sweep.png" target="_blank"><img id="benchimg" src="parallel-sweep.png" alt="parallel throughput sweep chart" style="max-width:100%;border:1px solid #232a36;border-radius:8px" onerror="document.getElementById('benchsec').style.display='none'"></a>
  </section>
</main>
<script>
function pill(state){const c=state==='idle'?'idle':(state==='busy'?'busy':'bad');return `<span class="pill ${c}">${state}</span>`;}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fmtK(n){ if(n==null) return '–'; return n>=1000? (n/1000).toFixed(1)+'k' : String(n); }
function activityHtml(a){
  if(!a) return '<span class="sub">–</span>';
  const active = a.active || [];
  const lines = active.map(s=>{
    if(s.phase==='prefill'){
      const pct = s.fresh? Math.round(100*s.processed/s.fresh):0;
      return `${pill('busy')} prefill <span class="bar"><i class="busy" style="width:${pct}%"></i></span> ${fmtK(s.processed)}/${fmtK(s.fresh)} tok (${pct}%)`;
    }
    if(s.phase==='decode'){
      return `${pill('busy')} decode <span class="sub">ctx ${fmtK(s.n_prompt)} tok</span>`;
    }
    return `${pill('busy')} working`;  // busy per /metrics but /slots detail unavailable
  });
  const head = lines.length? lines.join('<br>') : pill('idle')+' idle';
  const sub = [];
  // Throughput gauges from /metrics linger at their last-request value after a
  // request finishes, so only surface them while actually busy — otherwise an
  // idle model looks like it's still prefilling/decoding.
  if(a.busy){
    if(a.prompt_tps) sub.push(`prefill ${Math.round(a.prompt_tps)} t/s`);
    if(a.decode_tps) sub.push(`decode ${Math.round(a.decode_tps)} t/s`);
    active.forEach(s=>{ if(s.n_prompt) sub.push(`slot ${s.id}: ${Math.round(100*s.cache/s.n_prompt)}% cached`); });
    if(a.deferred) sub.push(`${a.deferred} queued`);
  }
  return head + (sub.length? `<div class="sub" style="margin-top:4px">${sub.join(' · ')}</div>`:'');
}
function bar(label, pct, txt){
  const p = (pct!=null)? Math.max(0, Math.min(100, pct)) : 0;
  const cls = p>=90? 'bad' : (p>=70? 'busy' : '');
  return `<tr><td>${label}</td>`+
    `<td><span class="bar"><i class="${cls}" style="width:${p}%"></i></span> ${pct!=null?pct+'%':'–'}</td>`+
    `<td class="sub">${txt||''}</td></tr>`;
}
function lastVal(a){ for(let i=a.length-1;i>=0;i--){ if(a[i]!=null) return a[i]; } return null; }
function spark(vals, opts){
  opts = opts || {};
  const w = 150, h = 30, pad = 2;
  const nums = vals.filter(v=>v!=null);
  if(!nums.length) return '<svg class="spark" width="'+w+'" height="'+h+'"></svg>';
  let mn = (opts.min!=null)? opts.min : Math.min(...nums);
  let mx = (opts.max!=null)? opts.max : Math.max(...nums);
  if(mx<=mn) mx = mn + 1;
  const n = vals.length;
  const step = n>1? (w-2*pad)/(n-1) : 0;
  const y = v => (h-pad) - ((v-mn)/(mx-mn))*(h-2*pad);
  let d='';
  vals.forEach((v,i)=>{ if(v==null) return; const x=pad+i*step; d += (d? 'L':'M') + x.toFixed(1) + ' ' + y(v).toFixed(1); });
  const col = opts.color || '#4b8ce0';
  return '<svg class="spark" viewBox="0 0 '+w+' '+h+'" width="'+w+'" height="'+h+'" preserveAspectRatio="none">'+
         '<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.5"/></svg>';
}
function cell(vals, unit, opts){
  const cur = lastVal(vals);
  return '<td class="g">'+spark(vals, opts)+'<span class="cur">'+(cur!=null? cur+unit : '–')+'</span></td>';
}
async function refreshHistory(){
  try{
    const r = await fetch('history.json', {cache:'no-store'}); const d = await r.json();
    if(!d.count){ document.getElementById('history').innerHTML = '<span class="sub">collecting… (first sample within '+Math.round(d.interval)+'s)</span>'; return; }
    const mins = Math.round(d.count*d.interval/60);
    document.getElementById('histspan').textContent = '· last '+(mins>=1? mins+' min':d.count*d.interval+'s')+' ('+d.count+' pts @ '+Math.round(d.interval)+'s)';
    let html = '<table><tr><th>GPU</th><th>Util</th><th>Power</th><th>Temp</th><th>VRAM</th></tr>';
    d.gpus.forEach(g=>{
      html += '<tr><td>'+g.index+' '+esc(g.name||'')+'</td>'+
        cell(g.util,'%',{min:0,max:100,color:'#4b8ce0'})+
        cell(g.power,' W',{color:'#e6c04b'})+
        cell(g.temp,'°C',{color:'#e07f7f'})+
        cell(g.mem,'%',{min:0,max:100,color:'#7fce7f'})+'</tr>';
    });
    html += '</table>';
    html += '<table style="margin-top:10px"><tr><th>Host</th><th>CPU</th><th>RAM</th></tr>'+
      '<tr><td>system</td>'+
      cell(d.cpu,'%',{min:0,max:100,color:'#4b8ce0'})+
      cell(d.mem,'%',{min:0,max:100,color:'#b58ce0'})+'</tr></table>';
    document.getElementById('history').innerHTML = html;
  }catch(e){ document.getElementById('history').innerHTML = '<span class="sub">history error: '+esc(String(e))+'</span>'; }
}
async function refresh(){  try{
    const r = await fetch('status.json', {cache:'no-store'}); const d = await r.json();
    document.getElementById('sub').textContent = d.summary + '  ·  updated ' + new Date(d.timestamp*1000).toLocaleTimeString();
    // models
    let m = d.models;
    const mode = m.mode && m.mode!=='unknown' ? m.mode : null;
    document.getElementById('modebadge').textContent = mode ? '· mode: '+mode : '';
    function cfgCols(x){
      const p = x.parallel!=null ? x.parallel : (m.config&&m.config[x.model]? m.config[x.model].parallel : null);
      const cps = x.ctx_per_slot!=null ? x.ctx_per_slot : (m.config&&m.config[x.model]? m.config[x.model].ctx_per_slot : null);
      const ctx = x.ctx!=null ? x.ctx : (m.config&&m.config[x.model]? m.config[x.model].ctx : null);
      const ctxTxt = cps!=null ? (fmtK(cps) + (p>1? ' ×'+p : '')) : '–';
      return `<td>${p!=null? p : '–'}</td><td title="${ctx!=null? ctx+' total':''}">${ctxTxt}</td>`;
    }
    if(!m.reachable){ document.getElementById('models').innerHTML = pill('unreachable'); }
    else if(!m.loaded.length){ document.getElementById('models').innerHTML = pill('idle') + ` <span class="sub">${m.available} available</span>`; }
    else {
      document.getElementById('models').innerHTML =
        '<table><tr><th>Model</th><th>State</th><th>Par</th><th>Ctx/slot</th><th>Activity</th></tr>' +
        m.loaded.map(x=>`<tr><td>${esc(x.model)}</td><td>${pill(x.state==='ready'?'idle':'busy')} ${esc(x.state)}</td>${cfgCols(x)}<td>${activityHtml(x.activity)}</td></tr>`).join('') +
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
    // host (cpu/ram/disk)
    const h = d.host || {};
    if(h && (h.cpu_pct!=null || h.mem || (h.disk&&h.disk.length))){
      const rows = [];
      const cpuTxt = (h.cpu_pct!=null? h.cpu_pct+'%':'–') +
        (h.load? ` <span class="sub">load ${h.load.join(' / ')} · ${h.cpus} cores</span>`:'');
      rows.push(bar('CPU', h.cpu_pct, cpuTxt));
      if(h.mem){ rows.push(bar('RAM', h.mem.used_pct,
        `${(h.mem.used_mb/1024).toFixed(1)} / ${(h.mem.total_mb/1024).toFixed(1)} GB`)); }
      (h.disk||[]).forEach(dk=>{ rows.push(bar('Disk '+esc(dk.path), dk.used_pct,
        `${dk.used_gb.toFixed(0)} / ${dk.total_gb.toFixed(0)} GB`)); });
      document.getElementById('host').innerHTML =
        '<table><tr><th>Resource</th><th>Usage</th><th></th></tr>'+rows.join('')+'</table>';
    } else { document.getElementById('host').innerHTML = pill('unavailable'); }
    // comfyui
    document.getElementById('comfyui').innerHTML = d.comfyui.length ?
      d.comfyui.map(c=>`<div style="margin:4px 0">${esc(c.label)}: ${pill(c.state)}` +
        (c.state==='busy'?` <span class="sub">${c.running} running, ${c.pending} queued</span>`:'') + `</div>`).join('')
      : pill('none');
    // services (app-tier health + click-through links built from browser host)
    const svc = d.services || [];
    document.getElementById('services').innerHTML = svc.length ?
      '<table><tr><th>Service</th><th>State</th><th>Link</th></tr>' +
      svc.map(s=>{
        const p = s.up ? `<span class="pill idle">up</span>` : `<span class="pill bad">down</span>`;
        const code = s.code!=null ? ` <span class="sub">${s.code}</span>` : '';
        const link = s.port!=null ? `<a href="http://${location.hostname}:${s.port}" target="_blank" style="color:#4b8ce0">:${s.port}</a>` : '<span class="sub">–</span>';
        return `<tr><td>${esc(s.name)}</td><td>${p}${code}</td><td>${link}</td></tr>`;
      }).join('') + '</table>'
      : pill('none');
  }catch(e){ document.getElementById('sub').textContent = 'status service error: ' + e; }
}
refresh(); setInterval(refresh, 5000);
refreshHistory(); setInterval(refreshHistory, 15000);
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
        elif path in ("/history.json", "/history"):
            body = json.dumps(build_history()).encode("utf-8")
            self._send(200, body, "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        elif path in ("/parallel-sweep.png", "/bench-chart.png"):
            try:
                with open(BENCH_CHART, "rb") as f:
                    self._send(200, f.read(), "image/png")
            except OSError:
                self._send(404, b"chart not found", "text/plain")
        elif path == "/":
            html = _HTML.replace("__BENCH_DOC_URL__", BENCH_DOC_URL)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):  # silence per-request stderr logging
        pass


def main():
    threading.Thread(target=_activity_loop, daemon=True).start()
    if HISTORY_ENABLED:
        threading.Thread(target=_history_loop, daemon=True).start()
    else:
        print("[history] HISTORY_ENABLED=false — time-series disabled")
    if OWUI_API_KEY:
        threading.Thread(target=_banner_loop, daemon=True).start()
    else:
        print("[banner] OWUI_API_KEY not set — OWUI banner push disabled")
    if FAST_KEEPER_ENABLED:
        threading.Thread(target=_fast_keeper_loop, daemon=True).start()
    else:
        print("[keeper] FAST_KEEPER_ENABLED=false — fast keeper disabled")
    if QUIET_HOURS_ENABLED:
        threading.Thread(target=_quiet_hours_loop, daemon=True).start()
    else:
        print("[quiet] QUIET_HOURS_ENABLED=false — deep-idle window disabled")
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

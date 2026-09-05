#!/usr/bin/env python3
"""Launcher for the (vendored) joenorton/comfyui-mcp-server.

We keep the upstream clone at /srv/ai/src/comfyui-mcp-server *pristine* so it can be
updated with `git pull`. Upstream's server.py hard-codes the FastMCP bind host to
127.0.0.1, which is unreachable from the mcpo container. This launcher imports the
already-constructed `mcp` object and overrides the host (default 0.0.0.0) before
starting the streamable-http transport — no fork of upstream required.

Config via env (see comfyui-mcp.service):
  COMFYUI_URL              ComfyUI base URL (default http://127.0.0.1:8188)
  COMFY_MCP_WORKFLOW_DIR   workflow library dir
  FASTMCP_HOST             bind host for the MCP server (default 0.0.0.0)
  COMFY_MCP_PUBLIC_URL     browser-reachable ComfyUI base URL used only for
                           display/asset links (e.g. http://192.168.4.57:8188)
  COMFY_MCP_RETURN_MARKDOWN  when truthy, tools return a markdown image link for
                           inline display in mcpo/Open WebUI (not MCP ImageContent)
  COMFY_MCP_SRC            path to the upstream clone (default /srv/ai/src/comfyui-mcp-server)
  COMFY_MCP_AUTH_TOKEN       Bearer token for a login-gated ComfyUI (ComfyUI-Login);
                           injected into every request to the ComfyUI host.
  COMFY_MCP_AUTH_TOKEN_FILE  path to a file whose FIRST LINE is that token (e.g. the
                           secure instance's comfyui/login/PASSWORD). Preferred over
                           COMFY_MCP_AUTH_TOKEN so the secret never lands in a unit
                           file or env, and auto-tracks a password change. Ignored if
                           COMFY_MCP_AUTH_TOKEN is set.
"""
import os
import sys
import time
from urllib.parse import urlsplit

SRC = os.getenv("COMFY_MCP_SRC", "/srv/ai/src/comfyui-mcp-server")
sys.path.insert(0, SRC)
# publish-root detection uses cwd; run from the repo root like upstream expects.
os.chdir(SRC)


def _load_auth_token() -> str:
    """Bearer token for a login-gated ComfyUI, from env or a file's first line."""
    tok = os.getenv("COMFY_MCP_AUTH_TOKEN", "").strip()
    if tok:
        return tok
    path = os.getenv("COMFY_MCP_AUTH_TOKEN_FILE", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.readline().strip()
        except OSError as exc:  # missing/unreadable -> run unauthenticated (degraded)
            print(f"[comfyui-mcp] auth token file {path!r} unreadable: {exc}",
                  file=sys.stderr, flush=True)
    return ""


# The (vendored) upstream talks to ComfyUI with module-level requests.get/post/head
# calls (no shared Session, no auth header). A login-gated instance (ComfyUI-Login,
# our :8189 "secure" canvas) rejects every unauthenticated call with 401/redirect.
# Rather than fork upstream, wrap requests.Session.request to attach
# `Authorization: Bearer <token>` for requests aimed at the ComfyUI host — including
# the import-time availability probe (so the bridge starts NON-degraded on secure)
# and the server-side asset HEAD/GET used to size/preview outputs. Installed BEFORE
# `import server` for exactly that reason. No-op when no token is configured, so the
# open (:8188, auth-less) bridge behaves exactly as before.
_AUTH_TOKEN = _load_auth_token()
if _AUTH_TOKEN:
    import requests

    # Authenticate requests to EITHER the connection host (COMFYUI_URL, used for
    # queueing + history polling) OR the public/display host (COMFY_MCP_PUBLIC_URL,
    # used by the server-side asset HEAD/GET that sizes outputs and builds the inline
    # preview). Both point at the same login-gated ComfyUI, just via different
    # host:port (127.0.0.1:8189 vs 192.168.4.57:8189); without the public one those
    # asset fetches 401 and the preview/metadata silently drops.
    _AUTH_HOSTPORTS = set()
    for _u in (os.getenv("COMFYUI_URL", "http://127.0.0.1:8188"),
               os.getenv("COMFY_MCP_PUBLIC_URL", "")):
        _u = (_u or "").strip()
        if _u:
            _AUTH_HOSTPORTS.add(urlsplit(_u).netloc)
    _orig_request = requests.sessions.Session.request

    def _request_with_auth(self, method, url, *args, **kwargs):
        try:
            same_host = urlsplit(url).netloc in _AUTH_HOSTPORTS
        except Exception:
            same_host = False
        if same_host:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("Authorization", f"Bearer {_AUTH_TOKEN}")
            kwargs["headers"] = headers
        return _orig_request(self, method, url, *args, **kwargs)

    requests.sessions.Session.request = _request_with_auth
    print(f"[comfyui-mcp] Bearer auth enabled for ComfyUI at {sorted(_AUTH_HOSTPORTS)}",
          flush=True)

# Upstream server.py runs a ComfyUI availability check at *import* time and calls
# sys.exit(1) if ComfyUI is unreachable. That makes the bridge (and its :9000
# endpoint) disappear whenever ComfyUI is intentionally down — e.g. the quiet-hours
# window stops comfyui-open/secure to free the V100 overnight. When the bridge is
# gone, mcpo's "comfyui" streamable-http upstream fails to initialise at startup,
# which cancels mcpo's whole lifespan and leaves it spinning a CPU core at 100%
# (taking the healthy stdio tools down with it).
#
# To keep :9000 reachable at all times, start the bridge in a *degraded* mode when
# ComfyUI is down: neutralise the import-time hard-exit (and skip its retry sleeps)
# so the FastMCP server still comes up and serves the tool list. Tool *calls* will
# fail until ComfyUI returns, but mcpo connects cleanly and stays healthy. When
# ComfyUI comes back, calls succeed again (the client connects per request). This
# keeps the upstream clone pristine — no fork required.
_orig_exit, _orig_sleep = sys.exit, time.sleep
sys.exit = lambda *a, **k: None        # let module load past the availability guard
time.sleep = lambda *a, **k: None      # don't block on the 5-attempt backoff
try:
    import server  # noqa: E402  (module-level ComfyUI availability check runs on import)
finally:
    sys.exit, time.sleep = _orig_exit, _orig_sleep

# Upstream blocks inline for at most max_attempts=30 (~30s) polling ComfyUI history;
# on timeout it returns a "still running, use get_job(...)" handle. For a COLD render
# on the secure canvas (14GB+ model load off disk + sampling) 30s is not enough, so
# the tool hands back a job handle — and some models react by RE-QUEUEING the same
# workflow instead of polling, producing duplicate GPU jobs and a long poll loop.
# Extend the inline wait (COMFY_MCP_MAX_WAIT seconds, well under the client's ~300s
# tool timeout) so typical cold renders finish in the first call. Only overrides the
# upstream default of 30 (callers that pass an explicit value are left alone); no-op
# if the env is unset, so the open bridge is unchanged unless configured.
_max_wait = os.getenv("COMFY_MCP_MAX_WAIT", "").strip()
if _max_wait:
    import comfyui_client as _cc  # noqa: E402  (already imported via server)

    _MAX_WAIT = int(_max_wait)
    _orig_wait = _cc.ComfyUIClient._wait_for_prompt

    def _wait_for_prompt_longer(self, prompt_id, max_attempts=30):
        if max_attempts == 30:  # upstream default -> substitute our configured budget
            max_attempts = _MAX_WAIT
        return _orig_wait(self, prompt_id, max_attempts=max_attempts)

    _cc.ComfyUIClient._wait_for_prompt = _wait_for_prompt_longer
    print(f"[comfyui-mcp] inline wait extended to {_MAX_WAIT}s", flush=True)

server.mcp.settings.host = os.getenv("FASTMCP_HOST", "0.0.0.0")
_port = os.getenv("FASTMCP_PORT", "").strip()
if _port:
    server.mcp.settings.port = int(_port)

# Asset URLs (returned to clients for inline display) are built from the ComfyUI
# base URL. The MCP server connects to ComfyUI over localhost, but a browser
# rendering an image needs a LAN/Tailscale-reachable URL. COMFY_MCP_PUBLIC_URL
# overrides only the *display* base URL, leaving the connection URL untouched.
_public_url = os.getenv("COMFY_MCP_PUBLIC_URL", "").strip()
if _public_url:
    server.asset_registry.comfyui_base_url = _public_url.rstrip("/")

# FastMCP's streamable-http transport has DNS-rebinding protection that only trusts
# localhost by default, so it rejects mcpo's "Host: host.docker.internal:9000" header
# with 421 Misdirected Request. Allow the hosts mcpo/agents use to reach us. This
# bridge is auth-less on the private LAN (like ComfyUI), so the protection adds little.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

_allowed = os.getenv(
    "COMFY_MCP_ALLOWED_HOSTS",
    "host.docker.internal:9000,127.0.0.1:9000,localhost:9000",
).split(",")
_allowed = [h.strip() for h in _allowed if h.strip()]
server.mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=_allowed,
    allowed_origins=["*"],
)

server.mcp.run(transport="streamable-http")

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

# --------------------------------------------------------------------------- #
# UI-defaults workflow patches (see scripts/comfyui_workflow_import.py).
#
# The bridge advertises a tool parameter for every PARAM_ placeholder an imported
# workflow exposes, and at render time fills omitted params from the vendored
# DefaultsManager's HARDCODED image defaults (width/height 512, steps 20) — NOT
# from the values you set in the ComfyUI UI. That's why an omitted `steps` became
# 20 instead of your workflow's 8. It also marks any placeholder outside a small
# hardcoded set (e.g. input_image, megapixels, length) as *required*.
#
# These two class patches make imported workflows honor the UI:
#   1. Relax `required` so ONLY `prompt` and `input_image` are mandatory; every
#      other omitted knob falls back to the workflow's own captured UI value.
#      Must run BEFORE `import server` because tool signatures (required vs
#      optional) are frozen when the generation tools are registered at import.
#   2. Wrap `render_workflow` to (a) merge the workflow's per-file `meta.json`
#      `defaults` (the captured UI values) UNDER the caller-provided params so an
#      omitted knob uses the UI value instead of the hardcoded 512/20, and (b)
#      resolve `input_image`-kind params by uploading a URL / data-URI / bytes to
#      ComfyUI's /upload/image and substituting the stored filename.
# Toggle off with COMFY_MCP_UI_DEFAULTS=0.
# --------------------------------------------------------------------------- #
if os.getenv("COMFY_MCP_UI_DEFAULTS", "1").strip().lower() not in ("0", "false", "no", "off"):
    import io as _io
    import base64 as _base64
    from managers import workflow_manager as _wm  # noqa: E402

    _ALWAYS_REQUIRED = {"prompt", "input_image"}
    _COMFY_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")

    # --- (1) required-relax ------------------------------------------------- #
    _orig_extract = _wm.WorkflowManager._extract_parameters

    def _extract_parameters_relaxed(self, workflow):
        params = _orig_extract(self, workflow)
        for name, param in params.items():
            param.required = name in _ALWAYS_REQUIRED
        return params

    _wm.WorkflowManager._extract_parameters = _extract_parameters_relaxed

    # --- input_image upload helper ------------------------------------------ #
    def _upload_input_image(value):
        """Return a ComfyUI-input filename for a URL / data-URI / bare name."""
        import requests  # local import; requests already patched for auth above
        val = str(value).strip()
        if not val:
            return val
        # A bare filename with no scheme/path is assumed to already live in the
        # ComfyUI input dir (or a prior upload) -> pass through unchanged.
        if "://" not in val and not val.startswith("data:") and "/" not in val:
            return val
        try:
            if val.startswith("data:"):
                _, _, b64 = val.partition(",")
                raw = _base64.b64decode(b64)
                fname = "mcp_input.png"
            else:
                r = requests.get(val, timeout=30)
                r.raise_for_status()
                raw = r.content
                fname = os.path.basename(urlsplit(val).path) or "mcp_input.png"
            files = {"image": (fname, _io.BytesIO(raw), "application/octet-stream")}
            resp = requests.post(f"{_COMFY_URL}/upload/image", files=files,
                                 data={"overwrite": "true", "type": "input"}, timeout=60)
            resp.raise_for_status()
            j = resp.json()
            name = j.get("name", fname)
            sub = j.get("subfolder", "")
            return f"{sub}/{name}" if sub else name
        except Exception as exc:  # noqa: BLE001 - surface a clear render error
            print(f"[comfyui-mcp] input_image upload failed for {val!r}: {exc}",
                  file=sys.stderr, flush=True)
            return val

    # --- (2) render_workflow: merge UI defaults + resolve input images ------- #
    _orig_render = _wm.WorkflowManager.render_workflow

    def _render_with_ui_defaults(self, definition, provided_params, defaults_manager=None):
        merged = dict(provided_params or {})
        try:
            meta_path = self._safe_workflow_path(definition.workflow_id)
            meta = self._load_workflow_metadata(meta_path) if meta_path else {}
        except Exception:  # noqa: BLE001
            meta = {}
        ui_defaults = meta.get("defaults", {}) if isinstance(meta, dict) else {}
        param_meta = meta.get("params", {}) if isinstance(meta, dict) else {}

        # Fill omitted knobs from the workflow's captured UI values (seed stays
        # unset so the vendored render randomizes it).
        for name, param in definition.parameters.items():
            if name == "seed":
                continue
            if merged.get(name) is None and name in ui_defaults:
                merged[name] = ui_defaults[name]

        # Resolve image-kind params (upload URL/data-URI -> input filename).
        for name, info in param_meta.items():
            if isinstance(info, dict) and info.get("kind") == "image":
                if merged.get(name):
                    merged[name] = _upload_input_image(merged[name])

        return _orig_render(self, definition, merged, defaults_manager)

    _wm.WorkflowManager.render_workflow = _render_with_ui_defaults
    print("[comfyui-mcp] UI-defaults workflow patches active "
          "(required=prompt/input_image only; omitted knobs use meta.json UI values)",
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

# When markdown mode is on (COMFY_MCP_RETURN_MARKDOWN), the tool result already
# carries a browser-reachable markdown image link that Open WebUI renders inline.
# Upstream ALSO exposes a `return_inline_preview` boolean tool parameter that, when
# the model sets it True, embeds a ~16KB base64 data URI in the tool result. That
# blob is redundant for display, poisons the conversation context, and — accumulated
# across turns on a 35B model — balloons prompt-processing time into multi-minute,
# no-token-output "churn" (and 10-min upstream timeouts). Force inline preview OFF so
# only the compact markdown link is returned. `register_and_build_response` is
# imported by-name into the tool modules, so patch each already-bound reference (and
# the source) to override the flag. Set COMFY_MCP_KEEP_INLINE_PREVIEW=1 to opt out.
_markdown_on = os.getenv("COMFY_MCP_RETURN_MARKDOWN", "").strip().lower() in (
    "1", "true", "yes", "on")
_keep_preview = os.getenv("COMFY_MCP_KEEP_INLINE_PREVIEW", "").strip().lower() in (
    "1", "true", "yes", "on")
if _markdown_on and not _keep_preview:
    import tools.helpers as _helpers  # noqa: E402

    _orig_build = _helpers.register_and_build_response

    def _build_no_inline_preview(*args, **kwargs):
        kwargs["return_inline_preview"] = False  # drop the base64 blob
        return _orig_build(*args, **kwargs)

    _helpers.register_and_build_response = _build_no_inline_preview
    # Re-point the copies the tool modules imported at import time.
    for _modname in ("tools.generation", "tools.workflow"):
        _mod = sys.modules.get(_modname)
        if _mod is not None and hasattr(_mod, "register_and_build_response"):
            _mod.register_and_build_response = _build_no_inline_preview
    print("[comfyui-mcp] inline base64 preview suppressed (markdown link only)",
          flush=True)

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

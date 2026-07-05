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
  COMFY_MCP_SRC            path to the upstream clone (default /srv/ai/src/comfyui-mcp-server)
"""
import os
import sys

SRC = os.getenv("COMFY_MCP_SRC", "/srv/ai/src/comfyui-mcp-server")
sys.path.insert(0, SRC)
# publish-root detection uses cwd; run from the repo root like upstream expects.
os.chdir(SRC)

import server  # noqa: E402  (module-level ComfyUI availability check runs on import)

server.mcp.settings.host = os.getenv("FASTMCP_HOST", "0.0.0.0")

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

"""
title: ComfyUI Inline Images
author: bradrlaw (ai-server)
description: Deterministically embed ComfyUI images inline in chat. The ComfyUI MCP
    tools (via mcpo) return a browser-reachable image URL, but models often present it
    as a plain [link](...) instead of an embedded ![image](...) (so it doesn't render),
    or copy a placeholder host like "host"/"localhost" (so it renders broken). This
    outlet filter rewrites any ComfyUI "/view?...filename=..." link or bare URL in the
    assistant's reply into inline markdown image syntax AND normalizes placeholder /
    loopback hosts to the real, browser-reachable ComfyUI address — independent of the
    model.
version: 1.1.0
required_open_webui_version: 0.5.0
"""

import re
from urllib.parse import urlsplit, urlunsplit
from pydantic import BaseModel, Field

# Matches a ComfyUI download/view URL, e.g.
#   http://192.168.4.57:8188/view?filename=z-image-turbo_00030_.png&type=output
# Host/port are intentionally unconstrained so it works over LAN or Tailscale, and so
# we can also catch (and fix) placeholder hosts like "host" or "localhost".
_VIEW_URL = r"https?://[^\s)<>\]\"']+/view\?[^\s)<>\]\"']*filename=[^\s)<>\]\"']+"

# 1) [label](URL) -> ![label](URL)   (skip if already an image: negative lookbehind for '!')
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\(\s*(" + _VIEW_URL + r")\s*\)")

# 2) bare URL -> ![image](URL)   (skip if preceded by '(' i.e. already inside a md link/image,
#    or by a quote/equals i.e. inside HTML)
_BARE_RE = re.compile(r"(?<!\()(?<![\"'=])(" + _VIEW_URL + r")")

# 3) already an image ![label](URL) -> keep, but normalize a placeholder/loopback host
_IMG_RE = re.compile(r"(!\[[^\]\n]*\]\(\s*)(" + _VIEW_URL + r")(\s*\))")

# Hostnames that are not reachable from a user's browser and must be rewritten. "host"
# is the literal placeholder some models copy from tool-description examples.
_BAD_HOSTS = {"host", "localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(
            default=True, description="Rewrite ComfyUI image links into inline images."
        )
        comfyui_base_url: str = Field(
            default="http://192.168.4.57:8188",
            description=(
                "Browser-reachable ComfyUI base URL. Used to fix image URLs whose host "
                "is a placeholder/loopback (host, localhost, 127.0.0.1, "
                "host.docker.internal). Set to your LAN or Tailscale address."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def _fix_host(self, url: str) -> str:
        base = (self.valves.comfyui_base_url or "").strip()
        if not base:
            return url
        try:
            u = urlsplit(url)
            if u.hostname and u.hostname.lower() in _BAD_HOSTS:
                b = urlsplit(base)
                return urlunsplit((b.scheme, b.netloc, u.path, u.query, u.fragment))
        except ValueError:
            pass
        return url

    def _embed(self, text: str) -> str:
        if not text:
            return text
        text = _IMG_RE.sub(lambda m: f"{m.group(1)}{self._fix_host(m.group(2))}{m.group(3)}", text)
        text = _LINK_RE.sub(lambda m: f"![{m.group(1)}]({self._fix_host(m.group(2))})", text)
        text = _BARE_RE.sub(lambda m: f"![image]({self._fix_host(m.group(1))})", text)
        return text

    def outlet(self, body: dict, __user__: dict = None) -> dict:
        if not self.valves.enabled:
            return body
        for message in body.get("messages", []):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = self._embed(content)
            elif isinstance(content, list):
                # Multimodal content: rewrite text parts only.
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part["text"] = self._embed(part.get("text", ""))
        return body

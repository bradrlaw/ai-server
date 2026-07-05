"""
title: ComfyUI Inline Images
author: bradrlaw (ai-server)
description: Deterministically embed ComfyUI images inline in chat. The ComfyUI MCP
    tools (via mcpo) return a browser-reachable image URL, but models often present it
    as a plain [link](...) instead of an embedded ![image](...), so it doesn't render.
    This outlet filter rewrites any ComfyUI "/view?...filename=..." link or bare URL in
    the assistant's reply into inline markdown image syntax — independent of the model.
version: 1.0.0
required_open_webui_version: 0.5.0
"""

import re
from pydantic import BaseModel, Field

# Matches a ComfyUI download/view URL, e.g.
#   http://192.168.4.57:8188/view?filename=z-image-turbo_00030_.png&type=output
# Host/port are intentionally unconstrained so it works over LAN or Tailscale.
_VIEW_URL = r"https?://[^\s)<>\]\"']+/view\?[^\s)<>\]\"']*filename=[^\s)<>\]\"']+"

# 1) [label](URL) -> ![label](URL)   (skip if already an image: negative lookbehind for '!')
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\(\s*(" + _VIEW_URL + r")\s*\)")

# 2) bare URL -> ![image](URL)   (skip if preceded by '(' i.e. already inside a md link/image,
#    or by a quote/equals i.e. inside HTML)
_BARE_RE = re.compile(r"(?<!\()(?<![\"'=])(" + _VIEW_URL + r")")


def _embed(text: str) -> str:
    if not text:
        return text
    text = _LINK_RE.sub(r"![\1](\2)", text)
    text = _BARE_RE.sub(r"![image](\1)", text)
    return text


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(
            default=True, description="Rewrite ComfyUI image links into inline images."
        )

    def __init__(self):
        self.valves = self.Valves()

    def outlet(self, body: dict, __user__: dict = None) -> dict:
        if not self.valves.enabled:
            return body
        for message in body.get("messages", []):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = _embed(content)
            elif isinstance(content, list):
                # Multimodal content: rewrite text parts only.
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part["text"] = _embed(part.get("text", ""))
        return body

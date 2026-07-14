"""
title: Image Format Normalizer
author: bradrlaw (ai-server)
description: >
    Transcode chat-attached images that the native vision models can't decode into
    PNG before the request reaches the model. Open WebUI's frontend happily displays
    webp/avif/heic, but the llama.cpp multimodal loader (mmproj, e.g. fast-uncensored
    on the P100) decodes images with stb_image, which supports PNG/JPEG/BMP/GIF but
    NOT webp/avif/heic/tiff — so those fail with "Failed to load image or audio file".
    This inlet filter finds base64 image data-URLs of unsupported types in the
    outgoing messages and re-encodes them to PNG in place. It also fixes the Flux
    Kontext edit tool, which then receives a PNG.
version: 1.0.0
required_open_webui_version: 0.5.0
"""

import base64
import io
import re
from typing import Optional

from PIL import Image
from pydantic import BaseModel, Field

_DATA_URL_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(
            default=True, description="Transcode unsupported image formats to PNG."
        )
        convert_types: str = Field(
            default="webp,avif,heic,heif,tiff,tif",
            description="Comma-separated image subtypes (from the data-URL mime) that "
            "the vision model can't decode and should be converted to PNG.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _targets(self) -> set:
        return {
            t.strip().lower()
            for t in (self.valves.convert_types or "").split(",")
            if t.strip()
        }

    def _to_png(self, subtype: str, b64: str, targets: set) -> Optional[str]:
        if subtype.lower() not in targets:
            return None
        try:
            raw = base64.b64decode(b64)
            img = Image.open(io.BytesIO(raw))
            # Flatten alpha onto white so opaque case/product photos stay natural.
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    def _fix_url(self, url: str, targets: set) -> str:
        m = _DATA_URL_RE.match(url or "")
        if not m:
            return url
        subtype = m.group(1).split("/", 1)[-1]
        converted = self._to_png(subtype, m.group(2), targets)
        return converted or url

    def inlet(self, body: dict, __user__: dict = None) -> dict:
        if not self.valves.enabled:
            return body
        targets = self._targets()
        if not targets:
            return body
        for message in body.get("messages", []):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                img = part.get("image_url")
                if isinstance(img, dict) and isinstance(img.get("url"), str):
                    img["url"] = self._fix_url(img["url"], targets)
        return body

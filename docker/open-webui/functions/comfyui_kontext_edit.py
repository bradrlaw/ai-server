"""
title: ComfyUI Flux Kontext Image Edit
author: bradrlaw (ai-server)
description: >
    Edit an image you uploaded in chat using Flux.1 Kontext on the AI server's
    ComfyUI (open instance, idx1). MCP/mcpo tools can't receive an uploaded image
    (they only get the JSON args the model emits, and Open WebUI never forwards the
    image bytes — see open-webui/open-webui discussions/13355). This NATIVE Open
    WebUI Tool solves that: it pulls the most recent uploaded image straight out of
    the chat messages (the base64 data-URL Open WebUI already attached for vision),
    uploads it to ComfyUI's input dir, runs the tested Flux Kontext edit workflow
    with your instruction prompt, and returns the result as an inline image. The
    companion "ComfyUI Inline Images" filter renders the returned link.
version: 1.0.0
required_open_webui_version: 0.5.0
"""

import base64
import json
import random
import re
import time
import uuid
from typing import Awaitable, Callable, Optional

import requests
from pydantic import BaseModel, Field

# Flux Kontext single-image edit graph (ComfyUI API / "prompt" format). Mirrors the
# workflow validated on the AI server (flux_kontext_dev_basic_official.json): a real
# image edit, mean pixel diff ~38.8 vs input. Placeholders {IMAGE}/{PROMPT}/{SEED}/
# {STEPS}/{GUIDANCE} are filled at call time. weight_dtype=default (NOT a *_fast fp8
# path) because the V100 is sm_70 with no native fp8.
_GRAPH = {
    "37": {"class_type": "UNETLoader",
           "inputs": {"unet_name": "flux1-dev-kontext_fp8_scaled.safetensors",
                      "weight_dtype": "default"}},
    "38": {"class_type": "DualCLIPLoader",
           "inputs": {"clip_name1": "clip_l.safetensors",
                      "clip_name2": "t5xxl_fp8_e4m3fn_scaled.safetensors",
                      "type": "flux", "device": "default"}},
    "39": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "{IMAGE}"}},
    "42": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["10", 0]}},
    "124": {"class_type": "VAEEncode", "inputs": {"pixels": ["42", 0], "vae": ["39", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["38", 0], "text": "{PROMPT}"}},
    "177": {"class_type": "ReferenceLatent",
            "inputs": {"conditioning": ["6", 0], "latent": ["124", 0]}},
    "35": {"class_type": "FluxGuidance",
           "inputs": {"conditioning": ["177", 0], "guidance": "{GUIDANCE}"}},
    "135": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["6", 0]}},
    "31": {"class_type": "KSampler",
           "inputs": {"seed": "{SEED}", "steps": "{STEPS}", "cfg": 1,
                      "sampler_name": "euler", "scheduler": "simple", "denoise": 1,
                      "model": ["37", 0], "positive": ["35", 0],
                      "negative": ["135", 0], "latent_image": ["124", 0]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["31", 0], "vae": ["39", 0]}},
    "9": {"class_type": "SaveImage",
          "inputs": {"filename_prefix": "kontext_owui", "images": ["8", 0]}},
}

_DATA_URL_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)


class Tools:
    class Valves(BaseModel):
        comfyui_api_url: str = Field(
            default="http://host.docker.internal:8188",
            description="ComfyUI URL reachable from the Open WebUI container (open "
            "instance, idx1). Uses the docker host-gateway alias.",
        )
        comfyui_public_url: str = Field(
            default="http://192.168.4.57:8188",
            description="Browser-reachable ComfyUI base URL used in the returned image "
            "link. Set to your LAN or Tailscale address.",
        )
        steps: int = Field(default=20, description="KSampler steps for Kontext.")
        guidance: float = Field(default=2.5, description="FluxGuidance value (2.5-3.5).")
        timeout_secs: int = Field(
            default=300, description="Max seconds to wait for the edit to finish."
        )

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ helpers
    def _latest_uploaded_image(self, messages: list) -> Optional[bytes]:
        """Return raw bytes of the most recent image found in the chat messages.

        Open WebUI attaches uploaded images to the user turn as multimodal content
        parts: {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}.
        We scan newest-first so the edit targets the image the user just sent.
        """
        for msg in reversed(messages or []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in reversed(content):
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                url = (part.get("image_url") or {}).get("url", "")
                if not url:
                    continue
                m = _DATA_URL_RE.match(url)
                if m:
                    try:
                        return base64.b64decode(m.group(2))
                    except Exception:
                        continue
                if url.startswith("http"):
                    try:
                        r = requests.get(url, timeout=30)
                        r.raise_for_status()
                        return r.content
                    except Exception:
                        continue
        return None

    def _upload(self, api: str, data: bytes) -> str:
        name = f"kontext_owui_{uuid.uuid4().hex[:12]}.png"
        r = requests.post(
            f"{api}/upload/image",
            files={"image": (name, data, "image/png")},
            data={"overwrite": "true"},
            timeout=60,
        )
        r.raise_for_status()
        j = r.json()
        # ComfyUI may namespace the upload under a subfolder; LoadImage wants
        # "subfolder/name" when a subfolder is set.
        sub = j.get("subfolder") or ""
        fn = j.get("name", name)
        return f"{sub}/{fn}" if sub else fn

    def _build_graph(self, image_ref: str, prompt: str) -> dict:
        raw = json.dumps(_GRAPH)
        raw = (
            raw.replace('"{IMAGE}"', json.dumps(image_ref))
            .replace('"{PROMPT}"', json.dumps(prompt))
            .replace('"{SEED}"', str(random.randint(1, 2**31 - 1)))
            .replace('"{STEPS}"', str(int(self.valves.steps)))
            .replace('"{GUIDANCE}"', str(float(self.valves.guidance)))
        )
        return json.loads(raw)

    def _run(self, api: str, graph: dict) -> str:
        client_id = uuid.uuid4().hex
        r = requests.post(
            f"{api}/prompt",
            json={"prompt": graph, "client_id": client_id},
            timeout=60,
        )
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]

        deadline = time.time() + self.valves.timeout_secs
        while time.time() < deadline:
            h = requests.get(f"{api}/history/{prompt_id}", timeout=30)
            if h.status_code == 200:
                hist = h.json().get(prompt_id)
                if hist:
                    status = (hist.get("status") or {}).get("status_str")
                    if status == "error":
                        raise RuntimeError("ComfyUI reported an error running the edit.")
                    for node in hist.get("outputs", {}).values():
                        for img in node.get("images", []):
                            if img.get("type") == "output":
                                return img["filename"]
                    if status == "success":
                        break
            time.sleep(1.5)
        raise TimeoutError("Timed out waiting for the Kontext edit to finish.")

    # -------------------------------------------------------------------- tool
    async def edit_image(
        self,
        prompt: str,
        __messages__: list = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
    ) -> str:
        """
        Edit the image the user most recently uploaded in this chat, following a
        natural-language instruction. Use this whenever the user has attached an
        image and asks to change, restyle, add to, or remove something from it
        (e.g. "make it snow", "turn the car red", "remove the background").

        :param prompt: The edit instruction describing the desired change to the
            uploaded image (e.g. "change the red fox into a white arctic fox").
        :return: Markdown with the edited image, rendered inline in the chat.
        """

        async def emit(desc: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {"description": desc, "done": done},
                })

        async def emit_image(url: str):
            # Render the image via a "files" event (same mechanism OWUI's native
            # image-gen uses). It persists on message.files independently of the
            # streamed assistant content, so the model's reply can't overwrite it.
            if __event_emitter__:
                await __event_emitter__({
                    "type": "files",
                    "data": {"files": [{"type": "image", "url": url}]},
                })

        api = self.valves.comfyui_api_url.rstrip("/")
        public = self.valves.comfyui_public_url.rstrip("/")

        img = self._latest_uploaded_image(__messages__ or [])
        if not img:
            await emit("No uploaded image found.", done=True)
            return ("No image was found in this chat. Please upload the image you want "
                    "to edit, then ask again.")

        try:
            await emit("Uploading image to ComfyUI…")
            image_ref = self._upload(api, img)

            await emit(f"Editing with Flux Kontext ({self.valves.steps} steps)…")
            graph = self._build_graph(image_ref, prompt)
            filename = self._run(api, graph)

            # Fetch the result server-side and embed as a data URL so it renders
            # regardless of whether the browser can reach the ComfyUI host.
            view = f"{api}/view?filename={filename}&type=output"
            data_url = None
            try:
                ir = requests.get(view, timeout=60)
                ir.raise_for_status()
                mime = ir.headers.get("Content-Type", "image/png").split(";")[0]
                b64 = base64.b64encode(ir.content).decode()
                data_url = f"data:{mime};base64,{b64}"
            except Exception:
                data_url = None

            public_url = f"{public}/view?filename={filename}&type=output"
            await emit("Edit complete.", done=True)
            await emit_image(data_url or public_url)
            return ("The edited image has been generated and is now shown inline in "
                    "the chat. Briefly confirm the edit to the user; do NOT output any "
                    "image markdown or link yourself.")
        except Exception as e:
            await emit(f"Edit failed: {e}", done=True)
            return f"Image edit failed: {e}"

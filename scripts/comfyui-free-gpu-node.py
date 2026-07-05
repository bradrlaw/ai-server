"""
free_gpu — ComfyUI server hook that frees the V100 automatically before a run.

Problem: ComfyUI (image gen) and llama-swap (LLM serving) both want the V100.
For a family setup nobody should have to SSH in and unload models by hand.

Solution: an aiohttp middleware on ComfyUI's PromptServer that, whenever a
generation is queued (POST /prompt), asks llama-swap to unload only the model(s)
occupying the card ComfyUI needs, so the GPU is free. It only acts on the
*generate* action — not on page loads — so simply opening the ComfyUI tab does
NOT evict anything; the card is freed only when someone clicks "Queue".

ComfyUI is pinned to a single V100 (idx1 = the `coding` card). `chat` (idx2) and
`fast` (P100) live on OTHER cards and never conflict, so we keep them loaded and
unload only the idx1 occupant via llama-swap's per-model unload endpoint
(POST /api/models/unload/<model>). Anything not in FREE_GPU_KEEP is unloaded; the
kept models stay responsive throughout image generation. Unloaded models reload
on demand the next time they're used.

Config via env (set in comfyui.service):
  LLAMASWAP_URL   default http://127.0.0.1:9090
  FREE_GPU_PATHS  comma list of request paths that trigger a free (default /prompt)
  FREE_GPU_KEEP   comma list of models to KEEP loaded (default "chat,fast" — the
                  idx2 + P100 models that don't share ComfyUI's card)
"""
import os
import logging

log = logging.getLogger("free_gpu")

LLAMASWAP_URL = os.environ.get("LLAMASWAP_URL", "http://127.0.0.1:9090").rstrip("/")
TRIGGER_PATHS = tuple(
    p.strip() for p in os.environ.get("FREE_GPU_PATHS", "/prompt,/api/prompt").split(",") if p.strip()
)
# Models to KEEP loaded through image generation — those on cards ComfyUI does
# NOT use (idx2 `chat` + P100 `fast`). Everything else running is unloaded so the
# idx1 V100 (the `coding` card) is freed.
KEEP_MODELS = frozenset(
    m.strip() for m in os.environ.get("FREE_GPU_KEEP", "chat,fast").split(",") if m.strip()
)

# ComfyUI expects custom nodes to export these; we have no nodes, just a hook.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


async def _free_gpu():
    """Unload only the llama-swap model(s) squatting on ComfyUI's card.

    Keeps FREE_GPU_KEEP models (default chat + fast, on idx2/P100) resident and
    unloads everything else running (idx1 `coding`, or a split `big`/`coder-next`)
    via llama-swap's per-model endpoint. Ignores failures — never blocks a run.
    """
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            # Inspect what's loaded; only unload models NOT in the keep-list.
            try:
                async with s.get(f"{LLAMASWAP_URL}/running", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    data = await r.json()
                    running = [m for m in data.get("running", []) if m.get("state") in ("ready", "loading", "starting")]
            except Exception:
                return  # llama-swap unreachable — nothing to free
            targets = [
                m["model"] for m in running
                if m.get("model") and m["model"] not in KEEP_MODELS
            ]
            if not targets:
                log.info(
                    "free_gpu: nothing to unload (running=%s, keeping=%s)",
                    [m.get("model") for m in running], sorted(KEEP_MODELS),
                )
                return
            log.info(
                "free_gpu: unloading %s before generation (keeping %s)",
                targets, sorted(KEEP_MODELS),
            )
            for name in targets:
                try:
                    async with s.post(
                        f"{LLAMASWAP_URL}/api/models/unload/{name}",
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        await r.read()
                except Exception as e:
                    log.warning("free_gpu: unload of %s failed (continuing): %s", name, e)
    except Exception as e:  # never block a generation because freeing failed
        log.warning("free_gpu: unload attempt failed (continuing anyway): %s", e)


def _install_middleware():
    try:
        import server  # ComfyUI's server module
        from aiohttp import web
    except Exception as e:
        log.warning("free_gpu: could not import ComfyUI server (%s); hook not installed", e)
        return

    inst = getattr(server.PromptServer, "instance", None)
    if inst is None or not hasattr(inst, "app"):
        log.warning("free_gpu: PromptServer.instance not ready; hook not installed")
        return

    @web.middleware
    async def free_gpu_mw(request, handler):
        try:
            if request.method == "POST" and request.path in TRIGGER_PATHS:
                await _free_gpu()
        except Exception as e:
            log.warning("free_gpu: middleware error (continuing): %s", e)
        return await handler(request)

    # Middlewares can still be appended before the server starts serving.
    inst.app.middlewares.append(free_gpu_mw)
    log.info("free_gpu: installed — will unload llama-swap models on POST %s", list(TRIGGER_PATHS))


_install_middleware()

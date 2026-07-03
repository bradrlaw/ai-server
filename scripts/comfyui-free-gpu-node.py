"""
free_gpu — ComfyUI server hook that frees the V100 automatically before a run.

Problem: ComfyUI (image gen) and llama-swap (LLM serving) both want the V100.
For a family setup nobody should have to SSH in and unload models by hand.

Solution: an aiohttp middleware on ComfyUI's PromptServer that, whenever a
generation is queued (POST /prompt), first asks llama-swap to unload its models
so the GPU is free. It only acts on the *generate* action — not on page loads —
so simply opening the ComfyUI tab does NOT evict the family's chat model; the
card is freed only when someone actually clicks "Queue".

The LLMs (coding/chat on the V100s, fast on the P100) reload on demand the next
time they're used, so no manual intervention is needed in either direction.

Config via env (set in comfyui.service):
  LLAMASWAP_URL   default http://127.0.0.1:9090
  FREE_GPU_PATHS  comma list of request paths that trigger a free (default /prompt)
"""
import os
import logging

log = logging.getLogger("free_gpu")

LLAMASWAP_URL = os.environ.get("LLAMASWAP_URL", "http://127.0.0.1:9090").rstrip("/")
TRIGGER_PATHS = tuple(
    p.strip() for p in os.environ.get("FREE_GPU_PATHS", "/prompt,/api/prompt").split(",") if p.strip()
)

# ComfyUI expects custom nodes to export these; we have no nodes, just a hook.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


async def _free_gpu():
    """Ask llama-swap to unload everything; ignore failures (LLMs may be absent)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            # Only unload if something is actually loaded, to avoid needless churn.
            try:
                async with s.get(f"{LLAMASWAP_URL}/running", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    data = await r.json()
                    running = [m for m in data.get("running", []) if m.get("state") in ("ready", "loading", "starting")]
            except Exception:
                running = None  # llama-swap unreachable — nothing to free
            if running is None:
                return
            if not running:
                log.info("free_gpu: no llama-swap models loaded; nothing to free")
                return
            names = ", ".join(m.get("model", "?") for m in running)
            log.info("free_gpu: unloading llama-swap models before generation: %s", names)
            async with s.get(f"{LLAMASWAP_URL}/unload", timeout=aiohttp.ClientTimeout(total=30)) as r:
                await r.read()
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

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

Reverse direction (idle watchdog): ComfyUI keeps its models cached in VRAM after
a run, which would keep the idx1 card occupied and block the daily `coding` model
from reloading. So a background task watches for inactivity: after FREE_GPU_IDLE_SECS
with no generation, it unloads ComfyUI's models + empties the CUDA cache to release
idx1, then warms FREE_GPU_RESTORE (default `coding`) back onto the card so the box
returns to its daily state (coding + chat + fast) with no manual step.

Config via env (set in comfyui.service):
  LLAMASWAP_URL       default http://127.0.0.1:9090
  FREE_GPU_PATHS      comma list of request paths that trigger a free (default /prompt)
  FREE_GPU_KEEP       comma list of models to KEEP loaded (default "chat,fast" — the
                      idx2 + P100 models that don't share ComfyUI's card)
  FREE_GPU_IDLE_SECS  seconds of no generation before ComfyUI's VRAM is released
                      and the daily model restored (default 300; 0 disables)
  FREE_GPU_RESTORE    comma list of models to warm back after freeing ComfyUI
                      (default "coding" — the idx1 daily model; empty = none)
  FREE_GPU_WAIT_SECS  after issuing an unload, how long to wait for the card to
                      actually free before proceeding (default 45; 0 disables the
                      wait). llama-swap's unload endpoint returns as soon as it
                      *signals* the model to stop, but the llama.cpp subprocess
                      needs a few more seconds to exit and release VRAM — starting
                      a generation before then races the teardown and OOMs.
  FREE_GPU_WAIT_RELEASE_MB  how much VRAM must be handed back (vs. the reading
                      taken just before unloading) for the card to count as freed
                      (default 6000). Guards against proceeding while the LLM is
                      still resident, without demanding the card be totally empty.
"""
import asyncio
import os
import logging
import time

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
# Idle watchdog: release ComfyUI's VRAM this long after the last generation, then
# warm the daily model(s) back. 0 disables the watchdog entirely.
IDLE_TIMEOUT = int(os.environ.get("FREE_GPU_IDLE_SECS", "300"))
CHECK_INTERVAL = 30  # how often the watchdog polls (seconds)
RESTORE_MODELS = [
    m.strip() for m in os.environ.get("FREE_GPU_RESTORE", "coding").split(",") if m.strip()
]
# After issuing an unload, wait up to this long for the card to actually free
# before letting the generation proceed (llama-swap's unload returns before the
# llama.cpp subprocess exits and releases VRAM). 0 disables the wait.
WAIT_SECS = float(os.environ.get("FREE_GPU_WAIT_SECS", "45"))
# How much VRAM (MB) must be handed back — measured against the free reading taken
# just before unloading — for the card to count as freed.
WAIT_RELEASE_MB = int(os.environ.get("FREE_GPU_WAIT_RELEASE_MB", "6000"))
# Physical GPU this ComfyUI instance is pinned to (CUDA_DEVICE_ORDER=PCI_BUS_ID is
# set in the unit, so this index matches nvidia-smi's numbering). First entry only.
_cvd = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")[0].strip()
PINNED_GPU = _cvd if _cvd and _cvd.lstrip("-").isdigit() else None

# Watchdog state. Start "already freed" so a box that never generates stays idle.
_last_activity = time.monotonic()
_freed_since_activity = True

# ComfyUI expects custom nodes to export these; we have no nodes, just a hook.
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


async def _gpu_free_mb():
    """Free VRAM (MB) on this instance's pinned GPU via nvidia-smi, or None if it
    can't be read. Runs the query off the event loop so we never block serving."""
    if PINNED_GPU is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "-i", PINNED_GPU,
            "--query-gpu=memory.free", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return int(out.decode().strip().splitlines()[0])
    except Exception:
        return None


async def _targets_still_live(targets):
    """True if any unload target is still in a live state per llama-swap /running."""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{LLAMASWAP_URL}/running", timeout=aiohttp.ClientTimeout(total=3)) as r:
                data = await r.json()
                live = {
                    m.get("model") for m in data.get("running", [])
                    if m.get("state") in ("ready", "loading", "starting")
                }
        return bool(set(targets) & live)
    except Exception:
        return False  # can't tell — don't hang the generation on this


async def _wait_for_release(targets, free_before):
    """Block until the card ComfyUI needs is actually free, or WAIT_SECS elapses.

    llama-swap's unload endpoint returns as soon as it *signals* the model to stop;
    the llama.cpp subprocess then takes a few seconds to exit and hand VRAM back to
    the driver. Proceeding immediately races that teardown and OOMs (the card still
    holds the ~20 GB LLM). We wait until BOTH: llama-swap no longer lists the target
    as live, AND the pinned GPU has handed back at least WAIT_RELEASE_MB (vs. the
    reading taken before unloading). If we can't read VRAM, we fall back to the
    /running signal plus a short settle.
    """
    if WAIT_SECS <= 0 or not targets:
        return
    start = time.monotonic()
    while time.monotonic() - start < WAIT_SECS:
        gone = not await _targets_still_live(targets)
        free_now = await _gpu_free_mb()
        if free_now is None:
            # No VRAM telemetry — rely on the unload signal plus a brief settle so
            # the subprocess has a moment to release before we start.
            if gone:
                await asyncio.sleep(2)
                log.info("free_gpu: unload signalled (no VRAM telemetry); proceeding")
                return
        elif gone and (free_before is None or free_now - free_before >= WAIT_RELEASE_MB):
            log.info(
                "free_gpu: card freed in %.1fs (free %s→%s MB)",
                time.monotonic() - start, free_before, free_now,
            )
            return
        await asyncio.sleep(0.5)
    log.warning(
        "free_gpu: card not confirmed free after %.0fs (free_before=%s MB); "
        "proceeding anyway", WAIT_SECS, free_before,
    )


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
            # Snapshot free VRAM before unloading so _wait_for_release can tell when
            # the card has actually handed the LLM's memory back.
            free_before = await _gpu_free_mb()
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
            # Wait for the teardown to actually release VRAM before returning, so the
            # generation doesn't start on a still-occupied card and OOM.
            await _wait_for_release(targets, free_before)
    except Exception as e:  # never block a generation because freeing failed
        log.warning("free_gpu: unload attempt failed (continuing anyway): %s", e)


def _free_comfyui_vram():
    """Unload ComfyUI's cached models and release the CUDA allocator cache so the
    idx1 V100 is actually free for the daily `coding` model to reload into."""
    try:
        import comfy.model_management as mm
        mm.unload_all_models()
        mm.soft_empty_cache(force=True)
        log.info("free_gpu: idle — unloaded ComfyUI models + emptied CUDA cache")
        return True
    except Exception as e:
        log.warning("free_gpu: failed to free ComfyUI VRAM: %s", e)
        return False


async def _restore_models():
    """Warm FREE_GPU_RESTORE model(s) back onto their card(s) via llama-swap, so
    the box returns to its daily resident state after an image session."""
    if not RESTORE_MODELS:
        return
    import aiohttp
    async with aiohttp.ClientSession() as s:
        for name in RESTORE_MODELS:
            try:
                body = {
                    "model": name,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                }
                async with s.post(
                    f"{LLAMASWAP_URL}/v1/chat/completions",
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as r:
                    await r.read()
                log.info("free_gpu: warmed %s back onto its card", name)
            except Exception as e:
                log.warning("free_gpu: failed to warm %s (continuing): %s", name, e)


def _generation_in_progress():
    """True if ComfyUI has a running or queued prompt. Used by the idle watchdog
    to avoid freeing VRAM during a long single generation (e.g. WAN video, which
    can run many minutes on one POST /prompt)."""
    try:
        import server
        inst = getattr(server.PromptServer, "instance", None)
        q = getattr(inst, "prompt_queue", None)
        if q is None:
            return False
        return q.get_tasks_remaining() > 0
    except Exception:
        return False


async def _idle_watchdog():
    """Background loop: once ComfyUI has been idle past IDLE_TIMEOUT, free its VRAM
    and restore the daily model. Runs at most once per idle period.

    A long single generation (e.g. a multi-minute WAN video) issues only ONE POST
    /prompt, so we must NOT treat "no new request" as idle while that job is still
    running — otherwise we'd unload the model mid-sampling. We therefore also skip
    (and reset the idle timer) whenever ComfyUI's prompt queue is non-empty.
    """
    global _freed_since_activity, _last_activity
    log.info(
        "free_gpu: idle watchdog running (timeout=%ds, restore=%s)",
        IDLE_TIMEOUT, RESTORE_MODELS or "none",
    )
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            if _generation_in_progress():
                # A job is still running/queued — defer the idle countdown so we
                # never free VRAM out from under an in-flight generation.
                _last_activity = time.monotonic()
                _freed_since_activity = False
                continue
            if _freed_since_activity:
                continue
            if time.monotonic() - _last_activity < IDLE_TIMEOUT:
                continue
            if _free_comfyui_vram():
                await _restore_models()
            _freed_since_activity = True  # don't repeat until the next generation
        except Exception as e:
            log.warning("free_gpu: idle watchdog error (continuing): %s", e)


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
                global _last_activity, _freed_since_activity
                _last_activity = time.monotonic()
                _freed_since_activity = False  # arm the idle watchdog
                await _free_gpu()
        except Exception as e:
            log.warning("free_gpu: middleware error (continuing): %s", e)
        return await handler(request)

    # Middlewares can still be appended before the server starts serving.
    inst.app.middlewares.append(free_gpu_mw)
    log.info("free_gpu: installed — will unload llama-swap models on POST %s", list(TRIGGER_PATHS))

    # Start the idle watchdog once the event loop is running (unless disabled).
    if IDLE_TIMEOUT > 0:
        async def _on_startup(app):
            app["_free_gpu_watchdog"] = asyncio.create_task(_idle_watchdog())
        inst.app.on_startup.append(_on_startup)
    else:
        log.info("free_gpu: idle watchdog disabled (FREE_GPU_IDLE_SECS=0)")


_install_middleware()

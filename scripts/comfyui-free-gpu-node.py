"""
free_gpu — ComfyUI server hook that frees the V100 automatically before a run.

Problem: ComfyUI (image gen) and llama-swap (LLM serving) both want the V100.
For a family setup nobody should have to SSH in and unload models by hand.

Solution: an aiohttp middleware on ComfyUI's PromptServer that, whenever a
generation is queued (POST /prompt), asks llama-swap to unload only the model(s)
occupying the card ComfyUI needs, so the GPU is free. It only acts on the
*generate* action — not on page loads — so simply opening the ComfyUI tab does
NOT evict anything; the card is freed only when someone clicks "Queue".

Each ComfyUI instance is pinned to one V100: comfyui-open and the on-demand
comfyui-qwen to idx1 (the `coding` card), comfyui-secure to idx2 (the `chat`
card). Models on the other cards never conflict, so we keep them loaded and
unload only this card's occupants via llama-swap's per-model unload endpoint
(POST /api/models/unload/<model>). Anything not in FREE_GPU_KEEP is unloaded; the
kept models stay responsive throughout image generation. Unloaded models reload
on demand the next time they're used.

Reverse direction (idle watchdog): ComfyUI keeps its models cached in VRAM after
a run, which would keep the card occupied and block the daily model from
reloading. So a background task watches for inactivity: after FREE_GPU_IDLE_SECS
with no generation, it unloads ComfyUI's models + empties the CUDA cache to release
the card, then warms FREE_GPU_RESTORE (e.g. `coding`) back onto it so the box
returns to its daily state (coding + chat + small) with no manual step.

Shared card (audit C8): comfyui-open and comfyui-qwen both render on idx1. If one
instance's idle timer warmed `coding` (~31 GB) while the other was rendering
(~29 GB), one of them would run out of VRAM. So comfyui-qwen never warms anything
(FREE_GPU_RESTORE is empty), and comfyui-open yields to it (FREE_GPU_YIELD_TO):
while comfyui-qwen.service is running, comfyui-open still releases its own cached
models after the idle window but does not warm `coding`. Once the unit stops,
comfyui-open warms `coding` back (after its own idle window, never mid-render).

Config via env (set in each comfyui-*.service unit):
  LLAMASWAP_URL       default http://127.0.0.1:9090
  FREE_GPU_PATHS      comma list of request paths that trigger a free
                      (default /prompt,/api/prompt)
  FREE_GPU_KEEP       comma list of models to KEEP loaded (default "chat,small"; each
                      unit lists the models on the cards it does NOT use)
  FREE_GPU_IDLE_SECS  seconds of no generation before ComfyUI's VRAM is released
                      and the daily model restored (default 300; 0 disables)
  FREE_GPU_RESTORE    comma list of models to warm back after freeing ComfyUI
                      (default "coding" — the idx1 daily model; empty = none)
  FREE_GPU_YIELD_TO   comma list of systemd units that share this card (default
                      none). While any of them is running, never warm
                      FREE_GPU_RESTORE; warm it once they have all stopped. Any
                      `systemctl is-active` state other than inactive/failed counts
                      as running, and so does a failed query (don't risk the OOM).
                      A unit systemd doesn't know reads as inactive, so the
                      watchdog warns about it once at startup (typo check).
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
# Models to KEEP loaded through image generation — those on cards this instance
# does NOT use. Everything else running is unloaded so this instance's card is freed.
KEEP_MODELS = frozenset(
    m.strip() for m in os.environ.get("FREE_GPU_KEEP", "chat,small").split(",") if m.strip()
)
# Idle watchdog: release ComfyUI's VRAM this long after the last generation, then
# warm the daily model(s) back. 0 disables the watchdog entirely.
IDLE_TIMEOUT = int(os.environ.get("FREE_GPU_IDLE_SECS", "300"))
CHECK_INTERVAL = 30  # how often the watchdog polls (seconds)
RESTORE_MODELS = [
    m.strip() for m in os.environ.get("FREE_GPU_RESTORE", "coding").split(",") if m.strip()
]
# Card-mate units (audit C8): while any of these systemd units is running, the idle
# watchdog never warms RESTORE_MODELS (the card-mate may be rendering); it warms
# them back once they have all stopped.
YIELD_UNITS = [
    u.strip() for u in os.environ.get("FREE_GPU_YIELD_TO", "").split(",") if u.strip()
]
# The only `systemctl is-active` states that mean the unit is NOT on the card.
_STOPPED_UNIT_STATES = frozenset({"inactive", "failed"})
_SYSTEMCTL_TIMEOUT = 5  # seconds
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

# Watchdog state. Start "released, nothing owed" so a box that never generates
# stays idle.
_last_activity = time.monotonic()
_released = True        # this instance's cached models released since the last activity
_restore_owed = False   # RESTORE_MODELS still need warming back
_yield_state = None     # last logged FREE_GPU_YIELD_TO state (log each change once)

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

    Keeps FREE_GPU_KEEP models (default chat + small, on idx2/idx0) resident and
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
    card is actually free for the daily model (or a card-mate) to use."""
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
                    r.raise_for_status()  # a 5xx (e.g. load failed) isn't "warmed"
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


def _mark_activity():
    """A generation was queued or is running: restart the idle countdown and owe
    a release (and a restore, if configured) once it has passed."""
    global _last_activity, _released, _restore_owed
    _last_activity = time.monotonic()
    _released = False
    _restore_owed = bool(RESTORE_MODELS)


async def _systemctl_words(*args):
    """stdout of `systemctl <args>`, split on whitespace. Raises on failure or
    after _SYSTEMCTL_TIMEOUT (killing the process); the exit code is ignored
    because `is-active` exits non-zero for stopped units."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_SYSTEMCTL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return out.decode().split()


async def _yield_blocker():
    """Why we must not warm RESTORE_MODELS now: e.g. "comfyui-qwen.service is
    active", or None if no FREE_GPU_YIELD_TO unit is running.

    Fails closed: if systemctl can't be read, the card-mate counts as running."""
    if not YIELD_UNITS:
        return None
    units = ",".join(YIELD_UNITS)
    try:
        states = await _systemctl_words("is-active", *YIELD_UNITS)
    except Exception as e:
        return f"state of {units} unreadable ({type(e).__name__})"
    if len(states) != len(YIELD_UNITS):
        return f"state of {units} unreadable (systemctl printed {states!r})"
    for unit, state in zip(YIELD_UNITS, states):
        if state not in _STOPPED_UNIT_STATES:
            return f"{unit} is {state}"
    return None


async def _check_yield_units():
    """Warn once at startup about FREE_GPU_YIELD_TO units systemd doesn't know: a
    misspelt unit reads as "inactive", so it would never hold the warm-up back."""
    if not YIELD_UNITS:
        return
    try:
        loads = await _systemctl_words("show", "-p", "LoadState", "--value", *YIELD_UNITS)
    except Exception as e:
        log.warning("free_gpu: couldn't check FREE_GPU_YIELD_TO units (%s)", type(e).__name__)
        return
    if len(loads) != len(YIELD_UNITS):
        log.warning("free_gpu: couldn't check FREE_GPU_YIELD_TO units (systemctl printed %r)", loads)
        return
    for unit, load in zip(YIELD_UNITS, loads):
        if load == "not-found":
            log.warning(
                "free_gpu: FREE_GPU_YIELD_TO unit %s is not installed (typo?); "
                "it will never hold back the warm-up", unit,
            )


async def _watchdog_tick():
    """One idle-watchdog pass: once ComfyUI has been idle past IDLE_TIMEOUT, free
    its VRAM and restore the daily model, at most once per idle period.

    A long single generation (e.g. a multi-minute WAN video) issues only ONE POST
    /prompt, so we must NOT treat "no new request" as idle while that job is still
    running — otherwise we'd unload the model mid-sampling. We therefore also skip
    (and reset the idle timer) whenever ComfyUI's prompt queue is non-empty.

    While a FREE_GPU_YIELD_TO card-mate is running we still release our own cached
    models (that only helps it), but the restore waits until it has stopped.
    """
    global _released, _restore_owed, _yield_state
    if _generation_in_progress():
        # A job is still running/queued — defer the idle countdown so we never
        # free VRAM out from under an in-flight generation.
        _mark_activity()
        return
    blocker = await _yield_blocker()
    if blocker != _yield_state:
        if blocker:
            log.info("free_gpu: %s; won't warm %s back meanwhile", blocker, RESTORE_MODELS or "anything")
        else:
            log.info("free_gpu: %s no longer running; idle restore resumes", ",".join(YIELD_UNITS))
        _yield_state = blocker
    if blocker and RESTORE_MODELS:
        # The card-mate evicts RESTORE_MODELS when it renders, so owe a warm-up for
        # when it stops, even if this instance never generates meanwhile.
        _restore_owed = True
    if time.monotonic() - _last_activity < IDLE_TIMEOUT:
        return
    if _restore_owed and not blocker:
        # Commit the state before awaiting the warm-up: a render queued while it
        # runs calls _mark_activity(), and that must survive (not be overwritten
        # when the warm-up returns).
        _released = True
        _restore_owed = False  # don't repeat until the next generation
        # The card-mate's state was read above; if it starts in the second before
        # the warm-up is sent, its first render's free_gpu evicts the loading model
        # again, and booting takes far longer than that. Not worth a cross-process lock.
        # Release our own cached models first; only warm if that worked.
        if _free_comfyui_vram():
            await _restore_models()
    elif not _released:
        _free_comfyui_vram()
        _released = True
        if _restore_owed:
            log.info("free_gpu: idle — not warming %s yet (%s)", RESTORE_MODELS, blocker)


async def _idle_watchdog():
    """Background loop: run _watchdog_tick every CHECK_INTERVAL seconds."""
    log.info(
        "free_gpu: idle watchdog running (timeout=%ds, restore=%s, yield_to=%s)",
        IDLE_TIMEOUT, RESTORE_MODELS or "none", YIELD_UNITS or "none",
    )
    await _check_yield_units()
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            await _watchdog_tick()
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
                _mark_activity()  # arm the idle watchdog
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

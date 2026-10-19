# ADR-0024: On-demand Qwen-Image 2.1 ComfyUI instance (`comfyui-qwen`, :8190)

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
Qwen-Image 2.1 is a new **unified generate + edit** image model (`Comfy-Org/Qwen-Image-2.1`).
Its official ComfyUI templates (`image_qwen_image_2_1_t2i`, `..._image_edit`,
`..._background_removal`) require **ComfyUI ≥ 0.37.0**, which ships the new
`comfy-kitchen` quant backend and `comfy-aimdo` "DynamicVRAM" allocator.

Our production ComfyUI (`/srv/ai/comfyui`, **v0.30.1**) is deliberately pinned: the
MiniMax-H3 native-fp16 custom nodes (`custom_nodes/minimax_h3_fp16_fix.py`,
ComfyUI-H3-Multishot) are tied to 0.30.1 internals and break on upgrade. Upgrading
the H3 tree in place to get Qwen-Image 2.1 is therefore not an option.

## Decision
Stand up a **separate, self-contained, on-demand, login-gated** ComfyUI 0.37.2
instance for Qwen-Image 2.1, isolated from the H3 tree but **sharing the model
store**, and wire it into the status page like the existing instances.

### Layout
- **App tree:** `/srv/ai/comfyui-qwen` (ComfyUI v0.37.2, gitignored).
- **venv:** `/srv/ai/venvs/comfyui-qwen` — **torch 2.8.0+cu128** (see below).
- **GPU:** idx1 (V100 #1), **shared with `comfyui-open`**. `CUDA_DEVICE_ORDER=PCI_BUS_ID`,
  `CUDA_VISIBLE_DEVICES=1`. The bundled `free_gpu` node (copied into
  `custom_nodes_qwen`) evicts the idx1 LLM occupant (`coding`) via llama-swap on
  each generate; `FREE_GPU_KEEP=chat,gemma-26b,small,small-uncensored`,
  `FREE_GPU_RESTORE=coding` (mirrors `comfyui-open`; now empty, see the
  2026-10-15 C8 addendum).
- **Port:** 8190. **Login-gated** via ComfyUI-Login in `custom_nodes_qwen` (own
  `login/PASSWORD`), mirroring `comfyui-secure`.
- **Shared:** the model tree, via `scripts/comfyui-qwen-extra-paths.yaml`
  (`base_path: /srv/ai/comfyui/`). **Separate:** custom_nodes, `output-qwen`/
  `input-qwen`/`temp-qwen`, `--user-directory`, DB.
- **Service:** `scripts/comfyui-qwen.service` (not enabled at boot; on-demand).

### torch 2.8.0 + cu128 (not 2.6/cu124)
`comfy-kitchen 0.2.35` requires **PyTorch ≥ 2.7** — torch 2.6 crashes at import
(`comfy_kitchen/backends/eager/conv3d.py` registers a `torch.library.custom_op`
with a `stride: list[int]` param that 2.6's `infer_schema` rejects). We use
**torch 2.8.0+cu128**, whose wheel still ships **sm_70 (Volta)** kernels
(`get_arch_list()` → `sm_70 … sm_120`). That is a property of torch 2.8's cu128
build, not of CUDA 12.x: torch 2.7's cu128 build and those from 2.11 on have no sm_70
(this line first said "only CUDA 13 dropped it"; see the 2026-10-15 addendum).
comfy-kitchen's optimized int8/fp8 CUDA kernels
stay **disabled** on Volta (it wants cu130) and fall back to eager — irrelevant
for our bf16 weights, which run the fp16 tensor-core path.

### Models: full bf16 (Option C)
Downloaded to the shared tree:
- `diffusion_models/qwen_image_2.1_bf16.safetensors` (14.2 GB)
- `text_encoders/qwen3vl_8b_bf16.safetensors` (17.5 GB) — the encoder the templates use
- `text_encoders/qwen3vl_8b_int8_convrot.safetensors` (9.35 GB) — **fallback**, kept
- `vae/qwen_image_2.1_vae_bf16.safetensors` (0.68 GB)

bf16 gives higher quality and runs the native fp16 tensor-core path on Volta;
`int8_convrot` eager-falls-back (no int8 HW, no Triton on cu128) and is ~2× slower
on the denoise loop (consistent with our earlier V100 quant benchmarks). The int8
encoder is retained as a VRAM-relief fallback.

### `--disable-dynamic-vram` is REQUIRED
ComfyUI 0.37's `comfy-aimdo` DynamicVRAM allocator **greedily reserves ~30 GB and
then OOMs** during the Qwen-Image 2.1 WanVAE decode — the sampler finishes, but the
VAE decode's upsample can't grow (at OOM the process holds 31.25 GiB while only
1.03 GiB is actually PyTorch-allocated; the rest is aimdo's un-released pool).
`--disable-dynamic-vram` switches to classic estimate-based offloading, which peaks
at **~29 GB / 31.7 GB** for full-bf16 1024² (~2.7 GB headroom) and completes cleanly.

### Status page
Auto-discovers via env — no `.py` change:
- `server-status.service`: `COMFYUI_URLS += ,qwen=http://127.0.0.1:8190` and
  `QUIET_COMFYUI_UNITS=comfyui-open,comfyui-secure,comfyui-qwen` (gives it a per-row
  Start/Stop button **and** quiet-hours VRAM release).
- `server-status-comfyui.sudoers`: added the 3-unit vector line (quiet hours) and a
  single-unit `comfyui-qwen` line (per-row button). Argument-vector-exact.

## Validation
Live headless t2i on idx1 (fox-in-snow prompt, 1024², 20 steps, euler/simple,
bf16 diffusion + bf16 encoder, `--disable-dynamic-vram`): **success**, valid image
(mean 122, std 67, full 0–251 range), peak **29.0 GB**. First attempts OOM'd only
because (a) idx1 still held the `coding` LLM (fixed by the `free_gpu` node) and
(b) DynamicVRAM was on (fixed by the flag).

## Consequences
- A second 0.37.x tree + venv (~app + torch cu128) to maintain separately from the
  H3 0.30.1 tree. The model store stays single-copy (shared).
- Qwen-Image 2.1 must run login-gated on :8190; it shares idx1 with `comfyui-open`,
  so the two on-demand image instances should not generate simultaneously (each
  evicts the other's idx1 LLM but not each other). (`comfyui-open` is always-on;
  its idle warm-up of `coding` now waits for this unit to stop, see the
  2026-10-15 C8 addendum.)
- When a 16 GB+ card returns to idx0 (see AGENTS.md), this instance could move off
  the shared idx1.

## Alternatives considered
- **Upgrade the H3 tree in place** — rejected: breaks the pinned H3 fp16 nodes.
- **int8 everything** (template default, ~17 GB VRAM) — rejected as the default:
  ~2× slower denoise on Volta. Kept the int8 encoder as a fallback only.
- **bf16 diffusion + int8 encoder** (Option B) — viable, but full bf16 (C) fits in
  29 GB once DynamicVRAM is disabled, so we kept the higher-quality encoder.

## Addendum 2026-10-15: quiet hours no longer starts `comfyui-qwen` every morning (audit C7)

**Bug.** Adding `comfyui-qwen` to `QUIET_COMFYUI_UNITS` (Status page, above) gave it
quiet-hours VRAM release, but the window end ran `systemctl start` on **every**
configured unit. Once quiet hours was unpaused, this on-demand instance would
have started each morning and held idx1 next to `comfyui-open`/`coding`. It
would also have started `comfyui-secure` while the encrypted volume was locked.
Quiet hours has been paused since 2026-09-05, so this never actually happened.

**Fix (`server-status-service.py`).**
- At window start the service stops only the units that are running, one
  `systemctl stop` per unit, and records which ones in
  `/srv/ai/var/status-service/quiet-comfyui-stopped`.
- At window end it restarts exactly those, one per unit.
- Per-unit commands match the existing single-unit sudoers lines, so the
  sudoers file is unchanged. The 3-unit line remains only for the API call
  without `unit`.
- The list survives a `server-status` restart mid-window, and a window end
  missed while the service was down is restored on the next start. A list from
  before the last reboot is ignored.

Covered by a stubbed harness: 22 checks, plus 7 mutants, all killed. Live
behaviour is unchanged until quiet hours is unpaused.

**Owner note (audit E2).** Quiet hours stays paused, and `comfyui-secure` keeps
running with `--fp16-unet` for now. The owner will change both from the status
page when ready. The window end clears fp16 on every instance, so the first
unpaused night turns fp16 off.

## Addendum 2026-10-15: Qwen renders aren't imported as bridge workflows (audit C16)

The workflow importer (`scripts/comfyui_workflow_import.py`, also behind the
status page's "ComfyUI workflow sync" panel) does not scan
`comfyui-qwen/output-qwen` by default. This is deliberate. The MCP bridges
render on `comfyui-open` and `comfyui-secure`, which run the older H3-pinned
ComfyUI tree without the Qwen-Image 2.1 nodes. An imported Qwen workflow would
be offered to chat as a tool and then fail at render time.

If a prefix only matches renders in that folder, the importer now says so,
instead of reporting that nothing matched. A workflow that doesn't need the new
nodes can still be imported by giving the render's full path. A stubbed harness
covers this with 19 checks and 7 mutants, all killed; the pre-change importer
fails 8 of the checks. A live dry run against the real folders gave the new
message.

Same pass: the `free_gpu` hook had drifted. The shared copy used by
open/secure was never refreshed after the 2026-08-24 `fast` → `small` rename.
This instance's copy (`custom_nodes_qwen/free_gpu.py`) was copied from that
stale file on 2026-09-25. Both now match `scripts/comfyui-free-gpu-node.py`.
Only comments and the unused `FREE_GPU_KEEP` default changed, because every
unit sets that variable. `install-comfyui-instances.sh` now installs both
copies, so they can't drift again.

## Addendum 2026-10-15: `comfyui-open` waits for `comfyui-qwen` before warming `coding` (audit C8)

**Bug.** Both idx1 instances had `FREE_GPU_RESTORE=coding`. Each instance's idle
watchdog warms `coding` (~30 GB) back onto idx1 five minutes after its own last
render, without checking the other instance. If `comfyui-open` went idle during a
Qwen render (~29 GB), it would load `coding` onto the same card and OOM the
render. The reverse happened when this instance went idle while `comfyui-open`
rendered.

**Fix (owner's design).** This instance is only ever started and stopped by
hand, so whether its service is running is the signal. There is no job-level
coordination.
- `comfyui-open.service` sets `FREE_GPU_YIELD_TO=comfyui-qwen.service`. Every
  30 s the watchdog runs `systemctl is-active` on that unit, which needs no sudo.
  While the unit is in any state other than `inactive`/`failed`, `comfyui-open`
  doesn't warm `coding`. It still releases its own cached models after the idle
  window. If the state can't be read (error, timeout, unexpected output), the
  hook fails closed and treats the unit as running.
- Seeing the unit running also marks a warm-up as owed, because Qwen renders
  evict `coding`. After the owner stops this unit, `comfyui-open` warms `coding`
  back within one 30 s check. It waits until it has itself been idle for
  `FREE_GPU_IDLE_SECS` and nothing is queued.
- This unit sets `FREE_GPU_RESTORE=` (empty), so it never warms anything back.
  Its watchdog only releases its own cached models. The owner chose this so
  that only the always-on instance restores `coding`.
- The owner first proposed that `comfyui-open` do nothing at all while this unit
  runs. We kept the release of its own models, and the owner agreed. Otherwise
  models cached by an earlier `comfyui-open` render would stay on idx1 for the
  whole Qwen session, and the Qwen renders would OOM anyway.
- Eviction on generate is unchanged on both instances. It only unloads LLMs,
  which never hurts the other instance on the card.
- `comfyui-secure` (idx2) sets no `FREE_GPU_YIELD_TO`. With the variable unset,
  the hook behaves exactly as before.

A code review of the change led to three hardening fixes in the same commit:
- At startup the watchdog runs `systemctl show -p LoadState` on the
  `FREE_GPU_YIELD_TO` units and warns about any that are `not-found`. A
  misspelt unit reads as `inactive`, so it would never hold back the warm-up.
- The watchdog now saves its "done" state before it awaits the warm-up. A
  render queued during the minute or so that `coding` takes to load used to be
  overwritten when the warm-up returned. That render then never got its own
  idle release and warm-up.
- A warm-up that gets an HTTP error from llama-swap, such as a failed load, is
  now logged as `failed to warm`. It used to be logged as `warmed ... back`.

Accepted residuals from that review:
- The unit's state is read about a second before the warm-up is sent.
- A failed own release isn't retried until the next render.
- The warm-up can be missed if `comfyui-open` restarts within 30 s of this
  unit stopping. `coding` then loads on first use.

**Not covered.**
- Rendering on both idx1 instances at once can still OOM (see Consequences).
- The status service warms `coding`/`chat` at the end of the quiet-hours window
  (`_exit_window`) and on every `server-status` start outside the window
  (`QUIET_WARM_ON_START`, which includes now, while quiet hours is paused).
  Neither path checks for this unit. Avoid restarting `server-status` during a
  Qwen render.
- A chat request for `coding` during a Qwen render still loads it onto idx1,
  because llama-swap loads models on demand.

**Tests.** Stubbed harness (fake `systemctl` on PATH, fake llama-swap HTTP server,
fake clock), run under both ComfyUI venvs: 77 checks pass, and 22 of 22
mutants are killed. Among the checks:
- the yield across every `is-active` state
- fail-closed on error, hang and a missing binary
- a warm-up owed when only this unit ran
- waiting out a render and the idle window after this unit stops
- no warm-up after a failed release
- a render queued during the warm-up
- an HTTP 5xx from the warm-up
- the startup typo warning
- one log line per state change

The harness also loads the hook with the `FREE_GPU_*` values parsed from the
three unit files. The hook's `systemctl` calls were also run once, read-only,
against the real `systemctl` as `brad`. A running `comfyui-qwen.service` held
back the warm-up, and a misspelt unit was warned about.

## Addendum 2026-10-15: only some cu128 wheels keep sm_70 (audit D5)

**Correction.** The torch section said cu128 keeps Volta because it is still CUDA 12.x,
and that only CUDA 13 dropped sm_70. PyTorch's build scripts say otherwise. torch 2.7's
cu128 build starts at sm_75. 2.8–2.10 put sm_70 back on purpose
(pytorch/pytorch#157517). From 2.11 the cu128/cu129 builds start at sm_75 again. Only
cu126 has kept sm_70 in every release. The full table is in
[`../optional-tools.md`](../optional-tools.md#which-torch-wheels-run-on-the-v100s-sm_70).

**What it means here.** torch 2.8.0+cu128 is still right for this instance (measured
2026-10-15: `sm_70 sm_75 sm_80 sm_86 sm_90 sm_100 sm_120`). But no script pins it,
ComfyUI's `requirements.txt` lists `torch` unpinned, and PyPI's default torch wheel has
been cu130 since 2.11. So anything that upgrades torch in `venvs/comfyui-qwen` (a bare
`pip install -U`, or a custom node that requires a newer torch) can land on a wheel with
no sm_70.
Keep the torch/torchvision/torchaudio trio where it is. Before restarting the unit after
an update, check that
`CUDA_VISIBLE_DEVICES= /srv/ai/venvs/comfyui-qwen/bin/python -c "import torch; print(torch._C._cuda_getArchFlags())"`
still lists `sm_70`.

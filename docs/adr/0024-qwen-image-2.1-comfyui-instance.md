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
  `FREE_GPU_RESTORE=coding` (mirrors `comfyui-open`).
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
(`get_arch_list()` → `sm_70 … sm_120`). cu128 is still CUDA 12.x, so it keeps
Volta (only CUDA 13 dropped it). comfy-kitchen's optimized int8/fp8 CUDA kernels
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
  evicts the other's idx1 LLM but not each other).
- When a 16 GB+ card returns to idx0 (see AGENTS.md), this instance could move off
  the shared idx1.

## Alternatives considered
- **Upgrade the H3 tree in place** — rejected: breaks the pinned H3 fp16 nodes.
- **int8 everything** (template default, ~17 GB VRAM) — rejected as the default:
  ~2× slower denoise on Volta. Kept the int8 encoder as a fallback only.
- **bf16 diffusion + int8 encoder** (Option B) — viable, but full bf16 (C) fits in
  29 GB once DynamicVRAM is disabled, so we kept the higher-quality encoder.

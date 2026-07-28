# ADR-0018: GPU upgrade evaluation — 16/24/32 GB cards, arch→CUDA→feature→bandwidth→chassis-fit

- **Status:** Accepted (evaluation record — no purchase committed)
- **Date:** 2026-07-27
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The Tesla P100 died (ADR-0017) and a 12 GB GTX Titan X (Maxwell **sm_52**) is a
temporary idx0 stopgap. The owner is weighing a permanent replacement / additional
card and asked, across the 16/24/32 GB tiers: what's newer than the V100s, does a
new card force **CUDA 13**, and would it unlock newer model formats / attention
(fp8, INT8/INT4 quant, **NVFP4/FP4**, FlashAttention-3, SageAttention-3)?

Fixed constraints that shape the answer (see also ADR-0002/0003/0009, memories):
- **One system-wide driver.** R580 (`580.173.02`) already advertises **CUDA 13.0**
  runtime *and* still supports Maxwell/Pascal/Volta → the driver is **not** the gate.
- **CUDA *toolkit* is the gate, per-build.** CUDA 13 **dropped sm_52/60/70**, so a
  CUDA-13-compiled binary **cannot target the Titan X or the V100s**. CUDA 12.9 still
  builds for sm_52/60/70 **and** supports Ampere (sm_86) / Ada (sm_89). Only
  **Blackwell (sm_120)** *requires* CUDA 13.
- **Chassis/thermal ethos:** AZZA Solano full-tower, blower/shroud-cooled cards on
  dedicated fans, power-capped ~175–200 W for longevity/noise (V100s at 175 W,
  HBM throttles ~85 °C). Open-air triple-slot 300–575 W cards fight this hard.
- **PCIe:** all PEG ports cap **x8** (no x16 available; V100s bifurcated x8/x8 off
  root port 00:03; idx0/bus01 slot trains x4). Single-GPU inference is unaffected.
- **Bandwidth reference:** the served V100s are **HBM2 ~900 GB/s**. LLM decode is
  bandwidth-bound, so "newer" ≠ "faster" unless bandwidth ≥ ~900 GB/s.
- **Ecosystem is CUDA-only** (llama.cpp CUDA build, Nunchaku, SageAttention,
  ComfyUI fp8 paths). AMD ROCm cards (e.g. W7800 32 GB) break the stack (Nunchaku
  has no ROCm) → excluded.

### Feature → hardware/toolchain gates (the core matrix)
| Capability | Min arch | Toolchain | On current V100/Titan? |
|---|---|---|---|
| FP16/BF16 tensor cores | Volta sm_70 | CUDA 12.x | V100 yes; Titan (sm_52) no |
| INT8 tensor GEMM (IMMA, `torch._int_mm`) | **Turing sm_75** | CUDA 12.x | **No** (Volta TC is FP16-only) |
| INT8/INT4 quant (INT8-Toolkit `convrot`, Nunchaku SVDQuant INT4) | sm_75+ (best sm_86/89) | CUDA 12.x | No |
| Native **fp8** + **FlashAttention-2** + **SageAttention-2** | **Ada sm_89** | CUDA 12.x | No |
| **FlashAttention-3** (TMA/WGMMA/native fp8) | **Hopper sm_90** (datacenter) | CUDA 13 | No — not buyable in a consumer card |
| **NVFP4 / FP4** weights **and** **SageAttention-3** FP4 attention | **Blackwell sm_120** | **CUDA 13 + newest cuDNN** | No |

Key takeaways: (1) "FlashAttention isn't a CUDA feature" — it's separate kernels
(Dao-AILab lib + cuDNN fused attention) gated by **arch**; the toolkit only lets the
newest kernels compile/dispatch. (2) **Ada gains nothing from CUDA 13** — its ceiling
(FA2 + SageAttn-2 + fp8 + INT8/INT4) is fully available on **CUDA 12.9**. (3) The
entire **FP4 tier — NVFP4 quant *and* SageAttention-3 attention — is one gate:
Blackwell + CUDA 13.**

## Decision
**Record the evaluation; do not commit a purchase.** Guidance captured for when the
owner decides:

1. **Default replacement for the idx0 Titan-X slot = a blower Ampere/Ada card on
   CUDA 12.9, no toolchain change.** Preferred picks by VRAM:
   - **16 GB → RTX A4000** (single-slot, 140 W, sm_86; retires the Maxwell card,
     transforms ComfyUI with INT8/INT4 + FA2; cleanest physical/power fit).
   - **24 GB → RTX A5000** (2-slot blower, 230 W, sm_86) — more headroom, same fit.
   - Add **`86`/`89`** to `CMAKE_CUDA_ARCHITECTURES` (currently `52;60;61;70`) and
     rebuild llama.cpp once; one unified binary still spans all cards.
2. **Only pursue Blackwell (sm_120) if NVFP4 / FP4-attention is an explicit goal.**
   Cheapest on-ramp = **RTX 5060 Ti 16 GB (~$429)**. Run ComfyUI on that card from a
   **separate CUDA-13 PyTorch venv**, `CUDA_VISIBLE_DEVICES`-pinned; the native
   llama.cpp V100 stack stays on CUDA 12.9. Two toolchains coexist under the one R580
   driver. This is tolerable *because* ComfyUI is a self-contained per-GPU process —
   it would be far more painful for the unified llama.cpp binary.
3. **A 24 GB card is not a V100 upgrade** (less VRAM than the existing 32 GB V100s) —
   treat 16/24 GB cards as **Titan-X replacement / ComfyUI + `fast`-model** cards.
   Only **32 GB** matches V100 capacity, and only **GDDR7 Blackwell** 32 GB beats
   V100 bandwidth (pulling into CUDA 13).

### Reference tables (bandwidth vs V100 ~900 GB/s; prices ≈ 2026, indicative)
**16 GB** — cheapest tier, best chassis fit, cheapest NVFP4 entry:
| Card | Arch | BW GB/s | Power | Form | CUDA |
|---|---|---|---|---|---|
| **RTX A4000** | Ampere 86 | 448 | 140 W | 1-slot blower | 12.9 |
| RTX 2000 Ada | Ada 89 | 224 | 70 W | 1-slot | 12.9 |
| RTX 4060 Ti 16G | Ada 89 | 288 | 165 W | 2-slot | 12.9 |
| RTX 4070 Ti Super | Ada 89 | 672 | 285 W | 3-slot OA | 12.9 |
| RTX 4080/Super | Ada 89 | 717–736 | 320 W | 3-slot OA | 12.9 |
| **RTX 5060 Ti 16G** | Blackwell 120 | 448 | 180 W | 2-slot | **13** |
| RTX 5070 Ti | Blackwell 120 | 896 ✅ | 300 W | 3-slot OA | **13** |
| RTX 5080 | Blackwell 120 | 960 ✅ | 360 W | 3-slot OA | **13** |

**24 GB** — Titan-X replacement w/ headroom (still < V100 32 GB):
| Card | Arch | BW GB/s | Power | Form | CUDA |
|---|---|---|---|---|---|
| RTX 3090 | Ampere 86 | 936 ✅ | 350 W | 3-slot OA | 12.9 |
| RTX 4090 | Ada 89 | 1008 ✅ | 450 W | 3-slot OA | 12.9 |
| **RTX A5000** | Ampere 86 | 768 | 230 W | 2-slot blower | 12.9 |
| RTX 4500 Ada | Ada 89 | 672 | 210 W | 2-slot blower | 12.9 |
| A10 | Ampere 86 | 600 | 150 W | 1-slot passive | 12.9 |
| L4 | Ada 89 | 300 | 72 W | 1-slot passive | 12.9 |
| RTX PRO 4000 Blackwell | Blackwell 120 | ~672* | ~140–275*W | 2-slot | **13** |

**32 GB** — only tier matching V100 capacity:
| Card | Arch | BW GB/s | Power | Form | CUDA |
|---|---|---|---|---|---|
| RTX 5000 Ada | Ada 89 | 576 ⚠️ | 250 W | 2-slot blower | 12.9 |
| RTX PRO 4500 Blackwell | Blackwell 120 | ~900+* | ~200 W | 2-slot blower | **13** |
| RTX 5090 | Blackwell 120 | 1790 ✅ | 575 W | 3-slot OA | **13** |

\* preliminary / not fully verified. OA = open-air. Note **V100S 32 GB / GV100 are
still Volta** (not newer); A100 = 40/80 GB; RTX PRO 5000 Blackwell = 48 GB.

## Consequences
- Positive: any Ampere/Ada pick retires the crippled Maxwell Titan X, unlocks the
  fp8/INT8/INT4 + FA2/SageAttn-2 ComfyUI stack, and stays on the existing CUDA 12.9
  unified build (one-line arch add). Clear, cheap, low-risk.
- Negative / trade-offs: **the recurring tension** — server-friendly blower cards are
  bandwidth-compromised (all < V100's 900 GB/s); the cards that beat the V100 are hot
  open-air 300–575 W parts that fight the cooling/power ethos. And the whole FP4 tier
  costs a second (CUDA-13) toolchain.
- Follow-ups / things to watch: if Blackwell is chosen, add a CUDA-13 ComfyUI venv
  (per-GPU pin) and a build note; re-verify PRO 4000/4500 Blackwell specs (marked \*)
  at purchase time; confirm the idx0 x8 slot trains as expected; update
  `gpu-fan-control.config.json` (blower card rejoins a PWM curve; drop `monitor_only`)
  and `power_limits`; rebuild llama.cpp with the added arch and restore MTP draft only
  on sm_70+ cards (Maxwell MTP crash per ADR-0017).

## Alternatives considered
- **Go straight to CUDA 13 for the whole box** — rejected; drops sm_52/60/70, orphaning
  the V100s and Titan X. CUDA 12.9 + R580 already runs a CUDA-13 binary side-by-side
  when a Blackwell card warrants it (ADR-0002/0003 stand).
- **RTX 5090 (32 GB, 1.79 TB/s)** — fastest and 2× V100 bandwidth, but 575 W triple-slot
  open-air; incompatible with the blower/power-cap chassis without a cooling/PSU
  rebuild. Deferred.
- **RTX 5000 Ada (32 GB)** — matches V100 *capacity* on CUDA 12.9, but 576 GB/s is
  **below** the V100 it would pair with → a decode downgrade; value is features, not
  speed. Only if fp8/features at 32 GB with zero toolchain change is the priority.
- **AMD Radeon PRO W7800 (32 GB, ROCm)** — rejected; breaks the CUDA-only stack
  (Nunchaku/SageAttention/llama.cpp CUDA build).
- **Used RTX 3090/4090 (24 GB, ≥900 GB/s)** — best raw decode-per-dollar, but
  open-air 350–450 W; only if bandwidth outweighs chassis/power fit.

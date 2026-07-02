# ADR-0003: Use CUDA 12.9 (not 13.x) for Pascal/Volta support

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
A prior (mis-guided) install put **CUDA Toolkit 13.3** on the system. The GPUs are
Tesla P100 (**sm_60**, Pascal) and Tesla V100 (**sm_70**, Volta). Verified via
`nvcc --list-gpu-code` that CUDA 13.3 only targets **sm_75 and newer** - i.e. CUDA
13 **removed** Pascal and Volta support, so it cannot build for these cards.

CUDA 12.x only *deprecated* (did not remove) Pascal/Volta, so it still builds for
sm_60/sm_70. **12.9** is the newest 12.x release.

The user's requirement: use the latest CUDA that still works, and ensure PyTorch,
vLLM, and llama.cpp all support it.

## Decision
Standardize on **CUDA Toolkit 12.9** (12.9.2-1) + **cuDNN 9** (`cudnn9-cuda-12`).

Verification performed before deciding:
- CUDA 12.9 `nvcc` targets sm_60 and sm_70 (12.x keeps Pascal/Volta).
- PyTorch ships **stable `cu129` wheels** (torch 2.8, 2.9.1, 2.10, 2.11, 2.12) -
  not nightly-only.
- llama.cpp (source build) and vLLM both support CUDA 12.9.

Install via toolkit-only metapackage per ADR-0002.

## Consequences
- Positive: newest usable CUDA; broad stable-wheel coverage; keeps the door open
  for current PyTorch releases.
- Negative: Pascal/Volta are deprecated in 12.x - a *future* CUDA (13+) will not
  support these GPUs at all, capping the long-term ceiling (hardware-bound anyway).
- Follow-up: build engines with `CUDA_ARCHITECTURES="60;70"`.

## Alternatives considered
- **Stay on CUDA 13.3** - rejected; does not support sm_60/sm_70 at all.
- **CUDA 12.6 / 12.8** - viable and slightly more "settled", but 12.9 also has
  stable PyTorch wheels and is the newest 12.x; user explicitly wanted the latest.

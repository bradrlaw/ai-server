---
name: v100-performance-scout
description: "Use when scanning the web for new techniques, models, quantization formats, or articles that could improve LLM/image/video inference performance on Tesla V100 (Volta, sm_70) GPUs, specifically for this server's llama.cpp/llama-swap and ComfyUI stack. Triggered on a weekly cron schedule; produces a reviewable kanban report, never changes server config directly."
version: 1.0.0
author: Copilot CLI (for bradrlaw/ai-server)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Research, GPU, CUDA, Volta, V100, llama.cpp, ComfyUI, Performance]
    related_skills: [blogwatcher, arxiv]
---

# V100 / Volta Performance Scout

## Overview

This server (`/srv/ai`, repo `bradrlaw/ai-server`) runs two Tesla V100-PCIE-32GB
(Volta, `sm_70`) GPUs as its primary serving tier, plus a GTX Titan X stopgap card.
New inference tricks, quantization formats, and model releases are overwhelmingly
tuned/benchmarked for newer architectures (Ampere/Hopper/Blackwell) and often
silently assume features Volta does not have. This skill's job is to periodically
search the web for developments that are *actually* relevant to Volta-class
hardware, filter out the noise, and hand the owner a short, reviewable list —
it never installs, configures, or restarts anything on the live server itself.

## When to Use

- Invoked automatically by the weekly cron job `v100-perf-scout`.
- Can also be run manually when the owner asks for a fresh sweep of V100/Volta
  news (e.g. "check for new V100 tricks", "any new quant formats for Volta").

## Hardware & software constraints (filter EVERYTHING through this list)

- **GPUs:** 2x Tesla V100-PCIE-32GB (Volta, compute capability `sm_70`), no NVLink
  (PCIe only). A GTX Titan X (Maxwell, `sm_52`, 12GB) also serves one slot but is a
  stopgap — don't bother with Maxwell-only tuning advice, Volta is the priority.
- **CUDA ceiling: 12.x only.** CUDA 13.x drops Volta/Pascal/Maxwell support entirely.
  Any article recommending CUDA 13+/driver versions tied to it is not applicable.
- **No FlashAttention-2, no fp8, no `*_fast` fp8 code paths.** sm_70 lacks the
  tensor-core features FA2 and fp8 kernels require. Use `sdpa`/standard attention.
  Treat any "just enable fp8" or "FA2 speedup" tip as N/A unless it has an explicit
  sm_70/Volta fallback path.
  - PyTorch wheels: cu124/cu126 keep sm_70 on every release; cu128/cu129 only on
    torch 2.8-2.10; cu130+ never ships sm_70. PyPI defaults to cu130 since torch
    2.11 — any "just pip install torch" advice needs an index-url check for Volta.
- **Serving stack:** llama.cpp + **llama-swap** router (native, not Docker) serving
  GGUF models (Qwen3.6/3.8 dense + MoE, some with embedded MTP/speculative-decode
  heads via `--spec-type draft-mtp`). ComfyUI (two instances: open + a LUKS-gated
  "secure" instance) for image (Flux, Qwen-Image, Krea2 Turbo) and video (MiniMax
  H3) generation. Look for things that help *these specific tools*, not vLLM/SGLang
  exclusives unless they have a llama.cpp/ComfyUI equivalent.
- **No NVLink** between the V100s — P2P/tensor-parallel tricks that assume NVLink
  bandwidth won't transfer; PCIe-only multi-GPU splitting (`--split-mode layer`
  in llama.cpp) is what's actually usable here.

## What to look for

Good candidates (non-exhaustive):
- New llama.cpp features/flags that help Volta specifically (quantization formats,
  speculative decoding improvements, `--split-mode`/`-sm` tuning, KV-cache quant
  options compatible with sm_70, ngram/MTP draft improvements).
- New GGUF quantization schemes (e.g. successors to Q6_K/Q8_K_XL/IQ formats) with
  published Volta/V100 benchmarks or no architecture-specific blockers.
- ComfyUI custom nodes or core features that reduce VRAM or runtime on
  Ampere-and-older cards (not Blackwell/Hopper-exclusive attention kernels).
- Power/thermal tuning tips specific to V100 PCIe SXM-less cards (useful context:
  this server already runs power caps via `gpu-fan-control`, HBM throttles ~85°C).
- Benchmarks or community reports explicitly naming V100/Volta/sm_70/Tesla.

Bad candidates (filter these OUT, or flag clearly as "not applicable, needs a
newer GPU"):
- fp8/FP4 quantization-only speedups with no sm_70 path.
- FlashAttention-2/3-only features.
- vLLM/SGLang/TensorRT-LLM exclusive features with no llama.cpp equivalent.
- Anything requiring CUDA 13+ or a driver that drops Volta.

## Research process

1. Search the web (news, release notes, forums, reddit) for recent (last ~2-4 weeks since
   the last run) developments in: llama.cpp/ggml-org release notes, ComfyUI
   release notes + popular custom-node repos, r/LocalLLaMA, Hacker News,
   Hugging Face trending GGUF/quantized model releases, unsloth/bartowski/other
   quantizer blogs.
2. For each candidate finding, explicitly check it against the constraints above
   before including it. If uncertain whether something runs on sm_70, say so
   rather than guessing — mark it "needs verification" rather than recommending it
   outright.
3. Before filing a new report, check the `v100-perf-scout` kanban board
   (`hermes kanban list --board v100-perf-scout`) for existing open cards on the
   same topic to avoid duplicate findings across weekly runs.
4. File findings as a single kanban task per run (not one per finding) on the
   `v100-perf-scout` board via `hermes kanban create`, titled with the date
   (e.g. "V100 scout — 2026-10-21"), containing for each finding:
   - What it is + source link + date.
   - Why it's relevant to this server's V100s specifically.
   - Rough effort/risk to try it (e.g. "needs a fresh llama.cpp build", "GGUF
     re-quant required", "just a flag change").
5. Do **not** install packages, edit configs, restart services, or touch any
   files under `/srv/ai` as part of this skill. This is research-only; all
   follow-through is a human decision made via the kanban card.

## Common Pitfalls

- Recommending a feature/benchmark without checking it's not Ampere/Hopper-only —
  always name the compute capability or GPU family explicitly in the finding.
- Suggesting CUDA 13+, fp8, or FlashAttention-2 without a clearly labeled "does
  NOT apply to this server's Volta cards" caveat if mentioned at all.
- Filing duplicate kanban cards for the same finding across multiple weekly runs
  — always check existing open cards first.
- Taking any direct action on the server (this skill is observe-and-report only).

## Verification Checklist

- [ ] Every finding explicitly states its V100/Volta/sm_70 applicability (or is
      marked "not applicable"/"needs verification").
- [ ] No CUDA 13+/fp8/FlashAttention-2-only recommendations presented as directly
      usable without a caveat.
- [ ] Existing `v100-perf-scout` kanban cards checked for duplicates before filing.
- [ ] Exactly one new kanban card filed for this run (or none, if nothing new was
      found — don't file an empty card).
- [ ] No server files/configs/services were modified by this skill run.

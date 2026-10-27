---
name: model-engine-scout
description: "Use when scanning for new LLM/VLM model releases, quantizations, and updates to the Strata engine or llama.cpp that fit this server's VRAM (2x32 GB V100 + 12 GB Titan X) and RAM (125 GB, ~72 GB pinned by Strata). Runs on a cron one hour after v100-performance-scout; produces a reviewable kanban report, never changes server config directly."
version: 1.0.0
author: Copilot CLI (for bradrlaw/ai-server)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Research, Models, Quantization, llama.cpp, Strata, VRAM, RAM, GGUF]
    related_skills: [v100-performance-scout, blogwatcher, arxiv]
---

# Model & Engine Update Scout

## Overview

Companion to `v100-performance-scout`. That skill watches *hardware tricks*; this one watches
*what to run and how*: new models and quantizations that fit this box, new Strata releases, and new
llama.cpp builds. It hands the owner a short, reviewable list and never installs, downloads,
builds, configures or restarts anything.

## When to Use

- Invoked by the cron job `model-engine-scout` (every run day, one hour after `v100-perf-scout`).
- Manually when the owner asks "any new models / quants / Strata or llama.cpp updates?".

## Budgets (filter EVERYTHING through these)

- **VRAM:** 2x Tesla V100-PCIE-32GB (idx1, idx2; 64 GB total, PCIe only, `sm_70`) for the main tier,
  plus a 12 GB GTX Titan X (idx0, Maxwell, stopgap, can't run sm_70 kernels) for `small`.
  Leave KV-cache and compute-buffer headroom (about 2 GB per card free at the chosen context).
- **RAM:** 125 GB host. Strata pins about 72 GiB of experts for the 125B Flash-Next, so a model that
  needs more than roughly 80 GB of host RAM plus the VRAM tier does not fit.
- **Disk:** 2 TB NVMe, owner cap of about 250 GB per model family on NVMe.
- **Context:** capped at 512K tokens. 1M-context claims are not a reason to switch.
- **Quant policy (owner decision): Q4_K_XL / Q4_K_M or Q8 only.** Never recommend Q3 or lower
  (IQ3_S, IQ2, IQ1_M, ...), and never IQ4_XS. Say which Q4/Q8 file you mean and its size.
- **Hardware limits:** CUDA 12.x only; no fp8, no FlashAttention-2, no FP4/NVFP4 or `*_fast` fp8
  paths (sm_70). A model or format that needs them is "not applicable".
- **Stack:** llama.cpp (production build b11819, `0693dbfb9`, 2026-10-22) behind llama-swap; the
  **Strata** engine (github.com/Niko1221/Strata, checkout at `src/strata`, last seen 2026-10-21) serves
  Qwen3.8-Flash-Next 125B MoE (UD-Q4_K_XL) as `flash`. Current roster: Qwen3.8-27B (`coding`, `big`),
  Qwen3.6-35B-A3B (`chat`), Gemma-4-12B (`small`), Qwen3-Coder-Next (`coder-next`), plus Gemma comparison models.

## What to look for

1. **Models:** new open-weight releases (Qwen, Gemma, Mistral, GLM, DeepSeek, gpt-oss, Llama and similar)
   that could replace or beat a roster slot at a size that fits the budgets above. Prefer MoE with few
   active parameters, models with MTP/draft heads, and strong coding/agentic/vision results. Note the
   architecture: llama.cpp must already support it (check release notes), or Strata must.
2. **Quants:** new Unsloth/bartowski/community GGUFs of roster models, imatrix or "dynamic" quant
   improvements, KL-divergence or perplexity comparisons for Q4_K_XL / Q8. Note exact file sizes.
3. **Strata:** new releases/tags/commits at github.com/Niko1221/Strata (release notes, changelog,
   issues). Flag: new model support, speed-ups on Volta, vision/GPU-encoder changes, bug fixes that
   affected us (parity, OOM, `--spec`, context limits), breaking config changes.
4. **llama.cpp:** release notes since b11819 (check the latest `b` tag). Flag: new model architectures,
   MTP/speculative improvements, MoE offload or `--n-cpu-moe`, KV-quant options on sm_70, server/API
   changes (tool calls, reasoning, vision/mmproj), CUDA regressions on Volta. Remember a new build is
   never pulled straight into production: it is built in a separate clone and A/B tested first.

## Research process

1. Search the web (release pages, Hugging Face trending and model cards, r/LocalLLaMA, Hacker News,
   unsloth/bartowski posts, GitHub release notes for ggml-org/llama.cpp and Niko1221/Strata) for items
   from the last ~2-4 weeks, or since the last card on the board.

   **Reddit and other sites that block plain fetches:** the normal web/fetch tools get a 403 from
   Reddit. Use the `browser-fetch` MCP tools instead: `reddit_search(query, subreddit="LocalLLaMA",
   sort="new", time="month")` to find posts, then `browser_fetch(url)` on a post URL to read the thread
   and its comments (about 5-15 s each, so fetch only the few most relevant posts). If the tools are
   unavailable, say so in the card and fall back to search snippets; do not invent thread content.
2. For each candidate, write down: parameter count and active parameters, the Q4_K_XL/Q8 file size,
   whether it fits VRAM and RAM (show the arithmetic), whether llama.cpp/Strata support it today,
   and whether sm_70 can run it. If you cannot verify a number, label it "unverified".
3. Check the board (`hermes kanban list --board model-engine-scout`) for open or recent cards and do
   not repeat a finding unless something changed.
4. File exactly one task per run on the `model-engine-scout` board with `hermes kanban create`,
   titled with the date (e.g. "Model/engine scout - 2026-10-25"), in three sections: **Models/quants**,
   **Strata**, **llama.cpp**. Each finding: what it is, source link and date, fit (VRAM/RAM/disk),
   support status, and effort/risk (e.g. "needs new llama.cpp build", "needs download of N GB",
   "config only"). End with up to 3 recommended next steps, ranked.
5. If nothing new qualifies, file nothing.
6. Do **not** download models, install packages, build, edit files under `/srv/ai`, or restart services.

## Common Pitfalls

- Recommending a quant outside the owner's policy (anything below Q4, IQ4_XS) or an fp8/FP4-only model.
- Counting only VRAM: Strata-style MoE offload also needs the RAM, and KV cache grows with context.
- Treating a headline benchmark as applicable without checking engine support and the sm_70 path.
- Suggesting an engine upgrade straight into production instead of an A/B in a separate clone.
- Duplicating a card from a previous run.

## Verification Checklist

- [ ] Every model finding shows Q4/Q8 file size and a VRAM + RAM fit statement.
- [ ] No Q3-or-lower, IQ4_XS, fp8, FP4 or FlashAttention-2-only recommendation without a "not applicable" label.
- [ ] Strata and llama.cpp versions compared against the current ones (b11819, Strata checkout of 2026-10-21).
- [ ] Board checked for duplicates; exactly one card filed (or none).
- [ ] No server files, configs, downloads or services were touched.

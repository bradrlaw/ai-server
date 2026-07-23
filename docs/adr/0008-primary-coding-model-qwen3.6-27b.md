# ADR-0008: Primary coding model = Qwen3.6-27B (dense), with Qwen3.6-35B-A3B (MoE) as fast alternative

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
We need a primary coding model for the two V100s (32 GB each, no NVLink; see
ADR-0005). The Qwen3.6 family is the current recommendation:

- **Qwen3.6-27B** — *dense*, hybrid architecture (`model_type: qwen3_5`,
  `Qwen3_5ForConditionalGeneration`): interleaved **linear-attention** (Gated
  DeltaNet / SSM-style) + periodic **full-attention** layers (every 4th), plus a
  vision encoder. 64 layers, 262K native context. Strong coding-agent scores
  (SWE-bench Verified 77.2, Terminal-Bench 2.0 59.3).
- **Qwen3.6-35B-A3B** — *MoE*, 35B total / ~3B active per token. Much faster
  per-token; slightly lower quality than the 27B dense on most coding benchmarks.

Engine support was verified in our llama.cpp build (b-4f31eedb0, 2026-06-30):
both `Qwen3_5ForConditionalGeneration` (dense) and
`Qwen3_5MoeForConditionalGeneration` (MoE) are registered in
`conversion/qwen.py` and the hybrid arch is implemented in C++
(`LLM_ARCH_QWEN35` / `LLM_ARCH_QWEN35MOE`, sharing the `qwen3next` linear-attn
path). The vision encoder is dropped for text-only GGUF — fine for coding.

Pre-built GGUFs exist (unsloth, bartowski, lmstudio-community), so no manual
`convert_hf_to_gguf.py` step is required.

## Decision
- Adopt **Qwen3.6-27B dense** as the **primary coding model**.
- Keep **Qwen3.6-35B-A3B MoE** as the **fast/low-latency alternative** for
  interactive or routing-adjacent use.
- Source GGUFs from **unsloth** (`unsloth/Qwen3.6-27B-GGUF`).
- Benchmark quants on the V100s before committing a serving config:
  - **Q6_K (~21 GB)** — best quality that fits **one** V100 (single-card serving).
  - **BF16 (~54 GB, sharded)** — requires **both** V100s (tensor split); upper
    bound on quality and the reference "dual-card-required" case.
  - Compare single-card vs `-sm layer` vs `-sm row`, at context depths 0 and 8192,
    via `/srv/ai/scripts/bench-qwen3.6-27b.sh`.

## Consequences
- Positive: strong, current coding model with verified llama.cpp support and
  ready-made GGUFs; dense 27B fits a single V100 at Q6_K for simple, fast serving.
- Negative: hybrid linear-attention arch is newer — watch for llama.cpp
  correctness/perf fixes. (The "MTP" GGUF variant is now adopted — see the
  2026-07-23 addendum below.)
- Follow-up: record TP=2 vs single-card numbers (feeds ADR-0005), then pick the
  serving quant + `-sm` mode per model.

## Alternatives considered
- **Qwen2.5-Coder-32B / Qwen3-32B (dense)** — solid but older; superseded by
  Qwen3.6 on coding-agent benchmarks.
- **Run the 35B-A3B MoE as primary** — rejected as *primary* (lower quality than
  the 27B dense) but retained as the speed option. **Benchmarked 2026-07-01: MoE
  Q6_K = 97.6 tg t/s, ~3.8× faster than dense 27B Q6_K (25.6). BF16 MoE (69 GB)
  does not fit 2×V100 (64 GB); Q6_K fits one card.**
- **Download BF16 safetensors and convert ourselves** — unnecessary; quality
  GGUFs already exist.

## Addendum (2026-07-23) — MTP self-speculative decode adopted

We wired up multi-token-prediction, so the earlier "skip MTP variants" note no
longer applies. The `coding` slot now serves `unsloth/Qwen3.6-27B-MTP-GGUF`
`Qwen3.6-27B-Q6_K.gguf` (byte-identical Q6_K weights + embedded `blk.64.nextn.*`
head, +0.35 GB) with `--spec-type draft-mtp --spec-draft-n-max 2`.

- **+79% single-stream decode** (22.7 → ~40.6 t/s, `n_max=2`) at identical weights,
  lossless (the main model verifies every drafted token), ~2–6% prefill cost. The
  dense 27B is bandwidth-bound with very high draft acceptance (~86–88%), so it
  gains far more than the MoE `chat` (+31%). Apples-to-apples on stock llama.cpp,
  one V100. Full data: docs/benchmarking.md "MTP on the `coding` model".
- **Context trade:** MTP's extra ~1 GB compute buffer means 200k OOMs, so ctx is
  capped at **180k (184320)** — near-full prefill peaks 32.02/32 GB (~0.75 GB free).
- MTP is a single-stream latency win, so the `agentic` mode (coding at `--parallel 2`)
  overrides back to the non-MTP file at 200k with spec off; `heavy-coding` keeps MTP
  on the single-slot interactive primary.

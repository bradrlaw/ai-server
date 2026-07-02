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
  correctness/perf fixes. Skip "MTP" GGUF variants unless we wire up
  multi-token-prediction speculative decoding.
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

# ADR-0004: llama.cpp as primary engine, vLLM secondary

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The GPUs are Volta (V100, sm_70) and Pascal (P100, sm_60). These predate the
features modern serving stacks optimize for:
- **No bf16, no FP8** on either card.
- **No FlashAttention-2 / Marlin quant kernels** (require sm_80+).
- **P100 has no Tensor Cores at all.**

`llama.cpp` has the best Volta/Pascal support of any engine (GGUF quantization,
multi-GPU split, OpenAI-compatible `llama-server`) and builds cleanly for
sm_60/sm_70. vLLM offers higher throughput but its Volta path is second-class
(fp16-only, no FA2/FP8/Marlin) and P100 is effectively unsupported; it may need a
from-source build against matching torch/CUDA.

## Decision
Use **llama.cpp as the primary inference engine** for initial bring-up and likely
for production on these GPUs. Evaluate **vLLM as a secondary** option on the V100s
(only) later, and benchmark it against llama.cpp before committing to it.

Build llama.cpp with `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="60;70"`.

## Consequences
- Positive: fastest path to working inference on this exact hardware; GGUF quants
  let large (e.g. ~70B) coding models fit across 2x V100 (64GB).
- Negative: llama.cpp throughput/batching is lower than vLLM for concurrent load.
- Follow-up: revisit if vLLM benchmarks clearly win on the V100s (ADR update).

## Alternatives considered
- **vLLM as primary** - rejected for now; weak Volta support, no P100 support.
- **TGI / SGLang / ExLlamaV2** - keep as future candidates; ExLlamaV2 needs its
  sm_70 support verified.

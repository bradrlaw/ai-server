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

## Update 2026-07-18 — vLLM V100 feasibility spike (confirmed runnable)

A throwaway venv spike **confirmed vLLM runs on a V100 (sm_70)** — it selected the
**XFormers** attention backend, loaded a model in fp16, and generated at ~90 tok/s.
This validates the "vLLM as V100-only secondary" decision. Verified recipe:

| Component | Pin | Note |
| --- | --- | --- |
| vllm | `0.6.6.post1` | last broadly Volta-friendly release line |
| torch | `2.5.1+cu124` | pulled transitively; runs on driver 580 / CUDA 12.9 |
| xformers | `0.0.28.post3` | provides the Volta (sm_70) attention path |
| **transformers** | **`4.47.1`** | ⚠️ 5.x removes the tokenizer API this vLLM uses (`all_special_tokens_extended`) → hard fail; keep `huggingface-hub` <1.0 |
| env | `VLLM_ATTENTION_BACKEND=XFORMERS`, `--dtype float16`, `CUDA_DEVICE_ORDER=PCI_BUS_ID`, `CUDA_VISIBLE_DEVICES=1[,2]` | fp16 only; no FA2 / FP8 / Marlin on sm_70 |

- **Ceiling caveat:** `0.6.6.post1` is a Dec-2024 release. The now-default vLLM **V1
  engine requires sm_80 attention** (FlashAttention/FlashInfer) and will **not** load
  on the V100. This pin is effectively the newest usable vLLM on this hardware until
  the GPUs change — pin it; do not `pip install -U`.
- **P100 (sm_60):** not tested; prebuilt wheels omit sm_60 and core kernels lack a
  Pascal path. Keep the P100 on llama.cpp.
- Spike venv kept at `/srv/ai/venvs/vllm-spike` (~6.6 GB) as a bring-up scaffold.
- **Still TODO before promoting vLLM to a real service:** benchmark throughput vs
  llama.cpp under batched load, and test `--tensor-parallel-size 2` across the two
  V100s (no NVLink → PCIe-bound).

## Alternatives considered
- **vLLM as primary** - rejected for now; weak Volta support, no P100 support.
- **TGI / SGLang / ExLlamaV2** - keep as future candidates; ExLlamaV2 needs its
  sm_70 support verified.

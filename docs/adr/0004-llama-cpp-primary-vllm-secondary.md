# ADR-0004: llama.cpp as primary engine, vLLM secondary

- **Status:** Accepted — **vLLM secondary track ABANDONED 2026-07-18** (see final update)
- **Date:** 2026-07-01 (superseding decision 2026-07-18)
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

## Update 2026-07-18 — TP=1 vs TP=2 benchmark (Qwen2.5-7B-Instruct, fp16)

Ran `scripts/vllm-bench.py` (fixed synthetic batched workload, `ignore_eos`) on
Qwen2.5-7B-Instruct fp16, XFormers backend, V100s capped at 175 W, no NVLink:

| Workload | TP=1 (1×V100) out tok/s | TP=2 (2×V100) out tok/s | TP=2 gain |
| --- | --- | --- | --- |
| Light (256×512in×128out) | 744.5 | 836.3 | +12% |
| Heavy (512×1024in×256out) | 587.9 | 794.3 | +35% |

- KV headroom: TP=1 = 14,934 blocks (58× concurrency); TP=2 = 46,307 blocks (181×).
- **TP=2 is sublinear** — no NVLink means the per-token attention all-reduce crosses
  PCIe and eats most of the second card's compute. The gain grows with load (+12% →
  +35%) because TP=2's real benefit is **~3× more KV cache** (deeper batching under
  concurrency), not raw single-stream speed.
- **Decision guidance:** for a model that fits one V100, run **two independent TP=1
  instances** (~1,490 tok/s aggregate) rather than one TP=2 instance (836). Reserve
  **TP=2 for models >32 GB or very high concurrency** needing the KV headroom —
  consistent with ADR-0005.
- ⚠️ **Power note:** a concurrent full-load dual-V100 run tripped the owner's UPS
  (combined draw). Server has since been moved off the UPS; keep the 175 W caps.

## Update 2026-07-18 — DECISION: abandon the vLLM secondary track

While setting up a vLLM-vs-llama.cpp comparison on the actual daily models, we found
that **the only vLLM release that runs on Volta (`0.6.6.post1`) is older than every
model in the roster** — it recognizes only `Qwen2ForCausalLM` / `Qwen2MoeForCausalLM`:

| Daily model | HF/GGUF arch | vLLM 0.6.6 support |
| --- | --- | --- |
| coding / big — Qwen3.6-27B | `qwen35` | ❌ unknown arch |
| coder-next — Qwen3-Coder-Next 80B-A3B | `qwen3next` (Gated-DeltaNet linear-attn MoE) | ❌❌ arch + linear-attn kernels are sm_80+ only |
| chat — Qwen3.6-35B-A3B | `qwen35moe` | ❌ |
| fast — Gemma-4-12B | newer gemma | ❌ |

Compounding constraints on Volta:
- **No working quant path** — Volta has no int8/FP8 tensor-core kernels; vLLM's Marlin /
  CUTLASS W8A8 / FP8 backends are all sm_80+. vLLM on V100 is effectively **fp16-only**,
  so a 27B needs TP=2 (≈50 GB) and cannot fit one card, and "Q8 in vLLM" is not a real
  test. llama.cpp GGUF quants are the *only* way a 27B fits a single V100.
- Getting Qwen3.x arch support requires vLLM ≥0.8.x, whose default **V1 engine needs
  sm_80 attention** (FlashAttention/FlashInfer) → won't load on the V100.

**Decision:** stop all vLLM work. **llama.cpp + llama-swap is the sole serving engine**
on this hardware for the foreseeable future. vLLM would only become viable with an
Ampere-or-newer GPU (sm_80+), at which point this ADR should be revisited. The Volta
feasibility findings above are retained as evidence; the `vllm-spike` venv and the
`Qwen2.5-7B` test model were removed to reclaim disk.

## Alternatives considered
- **vLLM as primary** - rejected for now; weak Volta support, no P100 support.
- **TGI / SGLang / ExLlamaV2** - keep as future candidates; ExLlamaV2 needs its
  sm_70 support verified.

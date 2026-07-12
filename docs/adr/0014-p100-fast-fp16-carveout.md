# ADR-0014 — Patch llama.cpp to disable FAST_FP16 on the Tesla P100 (sm_60)

* Status: Accepted
* Date: 2026-07-12
* Deciders: @bradrlaw (+ Copilot CLI)

## Context

Our P100 (idx0, sm_60) runs the `fast` and `fast-uncensored` models. llama.cpp's
CUDA backend has a `FAST_FP16_AVAILABLE` gate meaning "this GPU has fast fp16, so
run quality-sensitive math in fp16." The GTX 10-series / P40 (sm_61) was long ago
carved out of that gate, but the P100 (sm_60) never was, because GP100 is the one
Pascal chip with genuine 2:1 fp16 hardware (18.7 TF). "Can" was treated as
"should" and nobody measured the accuracy cost.

An external write-up (gist by apollo-mg; merged in llama-cpp-turboquant PR #212)
measured it against an fp32-arithmetic truth base (Qwen3.6-27B Q6_K, wikitext-2,
2048 ctx, 32 chunks, KLD over full logit distributions):

| build                     | median KLD vs fp32 truth | top-token agreement |
| ------------------------- | ------------------------ | ------------------- |
| stock llama.cpp on P100   | 0.002298                 | 96.53%              |
| 3-line patch              | 0.000001                 | 99.89%              |

That is roughly 2300x tighter, with ~1 in 29 next-token predictions changing
outright on stock. The reported speed cost at pp8192/tg32 @ depth 8192 was zero
(prefill within noise, decode +1.4% faster patched) - real prefill is bound by
cuBLAS GEMM and memory bandwidth, not the fp16 vector path.

We reproduced this locally on our own P100 (idx0, sm_60). Truth base = our
patched (fp32-clean) build; models scored against it with wikitext-2, 2048 ctx,
32 chunks, FA on (production path), pinned via `CUDA_DEVICE_ORDER=PCI_BUS_ID
CUDA_VISIBLE_DEVICES=0`. Base model Qwen3.5-9B-Q8_0 (Gemma-4-12B QAT gave a
broken raw-text PPL and was unusable as a truth base):

| build (local, P100)          | mean KLD vs fp32 truth | same top-token |
| ---------------------------- | ---------------------- | -------------- |
| stock llama.cpp on P100      | 0.012186               | 95.09%         |
| 3-line patch (sanity control)| 0.000000               | 99.997%        |

Stock diverges measurably (median KLD 0.0045, RMS Δp 3.03%, ~1 in 20 next-token
predictions flips); the patched build is bit-stable against its own fp32 base
(near-zero KLD), confirming the arithmetic error is real and the patch removes
it. This matches the external write-up's direction (our absolute numbers differ
because of a different base model / quant).

On sm_60 these three gates control: (1) flash-attention tile/vec kernels, (2) the
cuBLAS compute type for the quantized-weight prefill path (MMQ is hard-disabled on
sm_60 - GP100 lacks DP4A - so ALL quantized weights dequantize then GEMM, and this
flag chose an fp16 GEMM), and (3) mmvf f16-weight matrix-vector arithmetic.

Constraints: we build llama.cpp from source for sm_60;70 already, so a local patch
costs only a rebuild. This is our private build, not an upstream contribution.

## Decision

Extend the existing sm_61 (`610`) carveout to also exclude sm_60 (`600`) in the
three `FAST_FP16` gates in `ggml/src/ggml-cuda/common.cuh`:

* `FAST_FP16_AVAILABLE` macro: `... && __CUDA_ARCH__ != 610 && __CUDA_ARCH__ != 600`
* `fast_fp16_available()`: `... && ggml_cuda_highest_compiled_arch(cc) != 610 && ... != 600`
* `fast_fp16_hardware_available()`: `... && cc != 610 && cc != 600`

The patch is saved at `scripts/patches/p100-fast-fp16-carveout.patch` and must be
re-applied after any llama.cpp update, followed by a rebuild.

## Consequences

- Positive: P100 (`fast`, `fast-uncensored`) now runs fp32-clean arithmetic -
  far tighter agreement with the fp32 reference, no measured throughput loss.
- Negative / trade-offs: the llama.cpp source tree carries a local, uncommitted
  patch. A `git pull`/`checkout`/`reset` or fresh clone drops it; the tracked
  `.patch` file plus a rebuild restores it.
- Only the P100 is affected. sm_61 was already exempt (unchanged); Volta+ (our
  V100s, sm_70) do not hit these gates and are bit-identical before/after.
- Follow-ups: re-apply + rebuild after llama.cpp bumps. sm_62 (Jetson) shares the
  gate but is unmeasured - do not extend the carveout to it without measuring.

## Alternatives considered

- **Leave stock (fp16 fast path)** - rejected: measurable accuracy loss on the
  P100 for no speed benefit on this quant-heavy, bandwidth-bound workload.
- **Force fp32 via env/runtime flag instead of a source patch** - no such knob
  exists for these specific gates; they are compile-time / cc-derived.
- **Retire the P100 for serving** - rejected: 16 GB HBM2 at 732 GB/s is useful
  headroom for the always-on `fast` tier; the patch makes it fp32-accurate.

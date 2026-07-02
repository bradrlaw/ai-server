# ADR-0005: GPU workload split (P100 auxiliary, V100 primary)

- **Status:** Accepted
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
Three heterogeneous GPUs, no NVLink (all PCIe gen3 via host bridge, per
`nvidia-smi topo -m`):
- GPU0 = **P100 16GB** (sm_60, no Tensor Cores)
- GPU1 = **V100 32GB** (sm_70)
- GPU2 = **V100 32GB** (sm_70)

Mismatched memory and capability makes uniform pooling a poor fit; tensor
parallelism across dissimilar cards (V100+P100) is not sensible.

## Decision
Split roles by card:
- **P100 (16GB)** -> auxiliary/smaller models: **embeddings, TTS, STT, and agent
  routing.** Driven by llama.cpp / lightweight runtimes.
- **2x V100 (32GB each)** -> **primary coding models.** Either one model per card,
  or **tensor-parallel across the two V100s** for larger/long-context models.
- **Never** tensor-parallel across mismatched cards (V100 + P100).

## Consequences
- Positive: each card used where it's strongest; clean isolation of latency-
  sensitive aux services (P100) from heavy coding inference (V100s).
- Negative: V100 tensor parallelism is **PCIe-bandwidth bound** (no NVLink) - must
  benchmark TP=2 vs single-card per model before committing.
- Follow-up: pin GPUs per service via `CUDA_VISIBLE_DEVICES`; record TP benchmarks.

## Update (2026-07-01): TP benchmark evidence — Qwen3.6-27B
First TP=2 vs single-card benchmark (see `../server-setup.md` and ADR-0008):
- Q6_K fits **one** V100: single vs dual-layer is a **tie** (~25.6 tg t/s) — PCIe
  tensor split gives **no throughput gain** for a model that fits on one card.
- `-sm row` is **~4× slower** at prompt processing (PCIe sync, no NVLink) → use
  **`-sm layer` (default), never `row`** on this box.
- **Conclusion:** dual-V100 value is **capacity** (bigger weights/context), not
  speed. Default = one model per card; tensor-split (layer) only when a model or
  its context won't fit 32 GB.

**P2P measured (2026-07-01):** peer access between the two V100s is **enabled**,
but inter-GPU bandwidth is only **~5.2 GB/s** (PCIe via CPU host bridge; no
NVLink). TP therefore *works* but is communication-bound — good for **capacity**
(models >32 GB), not single-stream speed. vLLM NCCL TP=2 may help under batched
serving; retest at vLLM bring-up. See `../server-setup.md`.

## Alternatives considered
- **Pool all three GPUs uniformly** - rejected; capability/memory mismatch and no
  NVLink make this inefficient and fragile.

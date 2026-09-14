# ADR-0021: V100 homelab field-guide eval — DFlash2 and ECC-off deferred

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
A community "V100 Homelab Field Guide" (HF Space `KyleHessling1/v100-homelab-field-guide`,
[@kylehessling1](https://x.com/kylehessling1), measured Aug 2026) documents a single
**Tesla V100-32GB** (PCIe, sm_70) in a desktop running **Qwen3.8-27B Q4_K_M on llama.cpp**
with speculative decoding at ~60 tok/s. Same card family, engine, and model family as our
`coding`/`big` slots, so it is directly comparable — but it is a single-card, single-run,
**250 W desktop** rig, not a thermally-capped multi-GPU server. This ADR records the analysis
and the decision not to act on it for now.

Our relevant state at time of writing:
- GPUs: 2× Tesla V100-PCIE-32GB (idx1/idx2, sm_70) **capped 175 W / 200 W** for HBM thermals
  (HBM2 throttles ~85 °C), + GTX Titan X (idx0, sm_52, stopgap — see ADR-0017). No NVLink (PHB).
- Both V100s run **ECC Enabled**.
- `coding`/`big` use **MTP self-speculative decode**; `reasoning_effort` pinned `medium`.
- Active llama.cpp build's `--spec-type` already lists `draft-mtp` **and** `draft-dflash`.

## Decision
**Take no action** on the guide's two net-new levers. Specifically:

1. **Do not switch `coding`/`big` from MTP to DFlash2.** DFlash2 (llama.cpp mainline
   PR #27342, 27 Aug 2026, `--spec-type draft-dflash`) is already available in our binary,
   but the guide's own numbers show it only *wins* at full power + ECC-off, while at a
   **150–180 W cap MTP leads**. We cap V100s at 175/200 W for HBM thermals, so the guide's
   curve places us squarely in MTP's favourable region. Our existing MTP choice is correct
   for our power/thermal envelope.
2. **Keep ECC Enabled on the V100s** (do not run `nvidia-smi -e 0`). The guide reports
   ECC-off buys +12.5 % MTP / +4.3 % DFlash2 by relieving HBM bandwidth, and frames it as a
   homelab-quality tradeoff. For a family-facing, multi-user, long-running-agent box — and
   after losing a P100 to uncorrectable HBM2 ECC failure (ADR-0017) — ECC is our early-warning
   signal and worth the bandwidth tax.

## Consequences
- Positive: Config choices validated by an independent measured source; no change risk taken;
  ECC protection retained on hardware with a prior HBM failure in the fleet.
- Negative / trade-offs: We leave the guide's headline ~60 tok/s (250 W + ECC-off + locked
  clocks) on the table; our thermally-capped, ECC-on operating point is intentionally slower.
- Follow-ups / things to watch:
  - Revisit DFlash2 vs MTP **only if** power caps are ever lifted (e.g. improved cooling) —
    a quick head-to-head on the `coding` slot would then be worthwhile.
  - `draft-dflash` support is present in the build; no rebuild needed if we do revisit.

## Corroborations (no action needed, recorded for confidence)
The guide independently confirms several of our existing conventions:
- **`CUDA_DEVICE_ORDER=PCI_BUS_ID`** — its multi-minute "phantom hang" is our documented
  GPU-ordering gotcha (mixed-arch box JIT-compiling PTX for the wrong card).
- **Proprietary driver 580 + dual-arch build** — same lesson as our sm_52/60/70 build.
- **`reasoning_effort` is per-template**, valid only `xhigh|medium|low`; `high` throws a Jinja
  error — matches our Qwen3.8 tuning exactly.
- **Reasoning tokens ~halve throughput** — supports our `reasoning_effort=medium` pin.
- **Power-cap is workload-dependent** (MoE 150 W ≡ 250 W; dense+spec −15–30 %) — matches our
  own power-cap findings; the cap mainly taxes the dense+spec `coding` slot, not MoE slots.
- **KV cache is cheap** (8K→128K ≈ +7.5 GB on GQA) — consistent with our 96–160k contexts.
- **Auto-fit trap**: setting `-ncmoe`/`-ot`/`--tensor-split`/`-ngl` disables llama.cpp's
  `-fit` planner → OOM on device 0. Kept in mind for the hand-tuned `big` dual-V100 `-sm layer`.

## Alternatives considered
- **Switch to DFlash2 now** — rejected: at our 175/200 W caps MTP is the faster method per the
  guide's data; switching would likely regress or be neutral while adding a second draft model.
- **Disable ECC for bandwidth** — rejected: family/multi-user reliability plus a prior HBM ECC
  death (ADR-0017) make silent bit-flips an unacceptable trade for single-digit-% throughput.
- **Lift power caps to 250 W + lock clocks** — rejected: V100 HBM throttles ~85 °C under our
  cooling; the caps exist to stay under the thermal wall (see `gpu-fan-control`).

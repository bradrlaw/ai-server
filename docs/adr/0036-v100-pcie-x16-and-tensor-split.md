# ADR-0036: V100 PCIe x16 and `-sm tensor` for `big`: measurements

- **Status:** Accepted (measurements; no production change yet)
- **Date:** 2026-11-04
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
ADR-0033 left a TODO (`v100-pcie-x16`): both V100s ran at PCIe 3.0 x8 and Flash-Next streams experts over
PCIe, so x16 was *estimated* to cut long-prompt TTFT by 30-80%. At the same time llama.cpp (b11819) gained an
experimental `-sm tensor` split mode, and `-sm row` had been ~4x slower at prefill in July (benchmarking.md).
The question: is x16 (and tensor split) worth the hardware shuffle?

## Method
All on 2026-11-03/04, same build and weights, nothing else running, fans at 100%.
- **x8 baseline** (V100 idx1/idx2, Titan X in E6, boot disk on an Intel PCIe card): 2026-11-03.
- **x16** (V100s in E1/E4, Titan X removed, new WD SN350 NVMe in the M.2 slot): 2026-11-04. The V100s were
  enumerated as idx0/idx1, so runs used `V100_IDX=0,1` and a temporary `gpu: [0,1]` in `config/strata/flash.json`
  (restored afterwards). Power caps differed (200/175 W vs 175/225 W).
- Scripts: `scripts/flash-eval/pcie-baseline.py` (Flash-Next cold load/prefill with 1 Hz `dmon` rx/tx),
  `scripts/bench-big-split.sh` (`llama-bench` layer vs tensor), `scripts/bench-big-split-mtp.py`
  (`llama-server`, layer vs tensor, with/without `draft-mtp`). Data: `docs/data/big-split/`,
  `docs/data/flash-next/results/pcie-{x8,x16}.jsonl`.

## Results
**Flash-Next (`flash`), x8 -> x16**

| | x8 | x16 | change |
|---|---|---|---|
| cold load | 75 s | 55 s | 27% faster |
| TTFT 32k | 21.5 s | 16.3 s | 24% faster |
| TTFT 128k | 71.3 s | 60.2 s | 16% faster |
| TTFT 256k | 155 s | 135 s | 13% faster |
| decode (control) | 68.8 | 78.0 tok/s | within run-to-run noise (62-83) |

Gain is real but below the 30-80% estimate and shrinks with prompt length (compute-bound). Peak rx per V100 reached
~10-11 GB/s at x16 (x8 saturates near 6-7 GB/s).

**`big` (Qwen3.8-27B UD-Q8_K_XL, dual V100), `llama-bench`, tensor/layer ratio**

| | x8 | x16 |
|---|---|---|
| prefill 512 | 1.18x | 1.42x |
| prefill 4k | 0.95x | 1.14x |
| prefill 16k | 0.85x | 0.97x |
| decode | 1.61x | 1.61x |

Layer split barely changes with link width (+1-5% prefill). Tensor split decode is +61% at *both* widths; x16 only
repairs its long-prefill penalty.

**`big` via `llama-server`, x16, decode tok/s (code / prose / json), ctx 32k, f16 KV**

| config | code | prose | json | MTP accept | VRAM |
|---|---|---|---|---|---|
| layer, no MTP | 22.4 | 22.4 | 22.4 | - | 32.3 GB |
| tensor, no MTP | 35.8 | 35.8 | 35.8 | - | 31.8 GB |
| layer + MTP (current production) | 44.5 | 38.7 | 48.2 | 84% | 33.8 GB |
| **tensor + MTP** | **63.7** | **59.3** | **73.5** | 82% | 33.6 GB |

`draft-mtp` works with `-sm tensor`; combined it is ~1.5x production decode. A greedy JSON sample was identical in
all four configs (one small check, not a quality eval).

## Decision
- **No production change yet.** `big` stays `--split-mode layer`. `-sm tensor` is experimental upstream and was
  tested only for ~10 short requests per config at 32k ctx and `--parallel 1`.
- **Candidate:** switch `big` to `--split-mode tensor` after a soak (256k ctx fit, vision `--mmproj`, long agent
  sessions, stability) and a quality spot-check.
- x16 for the V100s is a modest win (13-27% on cold prefill/load; nothing on decode). It is worth keeping if it costs
  nothing, not worth sacrificing the boot drive or the Titan X for.

## Consequences
- **Hardware finding (board manual MS-7A21 p.16/33/39-41):** the M.2 is on CPU lanes (Gen3 x4) when slots allow.
  The "3-way" layout V100 E1 + V100 E4 + Titan E6 is the manual's recommended combination and puts the M.2 on the
  chipset (Gen2 x2) via `SE_USB_SW1 = SE1_65`, which disables SATA5/6/SATAe. In practice, with the Titan in E6 the
  M.2 never appeared and a V100 + Titan combo failed POST ("out of PCI lanes"); V100 x16/x16 + M.2 works without
  the Titan. ADR-0033's note that the M.2 is "PCIe 2.0 x2, no CPU lanes" is only true in that chipset mode.
- **GPU index hazard:** removing the Titan X renumbers the V100s to idx0/idx1. `gpu-fan-control` zones and power caps
  are by index, so the shroud V100 (UUID `10f1f24b...`) lost temperature control (zone mapped to idx2) and got the
  200 W idx0 cap. Fans were held at 100% manually for the tests. Any layout without the Titan at idx0 needs the
  config remapped before loading models.
- `scripts/verify-boot-migration.sh` (post-clone/GPU-move checks) records the expected layout.
- Follow-ups: soak `-sm tensor` + MTP; decide the final slot layout; re-run `pcie-baseline.py` if the layout
  changes; update ADR-0033's PCIe TODO as resolved.

## Alternatives considered
- **Estimate only (ADR-0033):** replaced by these measurements.
- **`-sm row`:** ~4x slower prefill in July; not retested.
- **Keep layer split:** safest, but gives up ~+50% `big` decode.

## Addendum 2026-11-04: long-context check (`scripts/bench-big-long-ctx.py`)

`big` Q8, MTP n=2, ctx 262144, f16 KV, V100s at x16, one request per depth (prefix reused between depths,
needle at the top, 220-token answer). Data: `docs/data/big-split/x16-longctx-summary.json`.

| depth (prompt) | layer+MTP prefill / decode | tensor+MTP prefill / decode |
|---|---|---|
| 16k | 867 tok/s / 32.1 | 883 tok/s / **48.5** |
| 64k | 600 / 27.8 | 654 / 29.1 |
| 128k | 371 / **22.9** | 429 / 18.9 |

Needle retrieved in 6/6. VRAM at 256k ctx: layer 51.7 GB, tensor 49.8 GB (both fit). MTP acceptance 56-63% on this
repetitive filler (vs 82-84% on the short natural prompts).

**Conclusion:** the tensor decode gain (+50% at short context) is gone by ~64k and reverses (-17%) at 128k.
Tensor prefill is 2-16% faster at depth. Switching `big` to `-sm tensor` helps short chats only; for long-context
agent/Copilot work it is a wash or worse, so production stays on `layer`. No config change.

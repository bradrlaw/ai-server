# ADR-0031: Vision (mmproj) on the `coding`, `big`, `chat` and `small` slots

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** @bradrlaw (+ Copilot CLI)
- **Related:** [ADR-0029](0029-qwen3.8-27b-migration.md) (Qwen3.8-27B slots),
  [ADR-0030](0030-llama-cpp-b11819-update.md) (llama.cpp b11819), [ADR-0007](0007-litellm-openai-api-gateway.md)

## Context
Pasting an image into Open WebUI on `coding` (and later `big`) failed with
`litellm.InternalServerError ... image input is not supported - hint: if this is unexpected, you may
need to provide the mmproj`. The Qwen3.8-27B and Qwen3.6-35B-A3B checkpoints and Gemma-4-12B are
multimodal, but llama.cpp only enables image input when a projector (`--mmproj`) is loaded, and only
`small-uncensored` had one. LiteLLM just forwards the backend error.

## Decision
Load an F16 projector (F16, not BF16: V100/Titan X have no fast BF16) in every interactive slot:

| Slot | Projector | Placement | Notes |
|---|---|---|---|
| `coding` | `qwen3.8-27b/mmproj-F16.gguf` (0.93 GB, unsloth/Qwen3.8-27B-GGUF) | GPU (idx1) | `--image-max-tokens 1024` |
| `big` | same file | GPU (idx1 of the pair) | `--image-max-tokens 1024` |
| `chat` | `qwen3.6-35b-a3b-mtp/mmproj-F16.gguf` (0.9 GB) | **CPU** (`--no-mmproj-offload`) | `--image-max-tokens 1024` |
| `small` | `gemma-4-12b/mmproj-F16.gguf` (0.18 GB) | GPU (idx0) | default image tokens |

`--image-max-tokens 1024` caps a Qwen image at about 1K tokens (a 1024x1024 image measured 1,042 prompt
tokens), which bounds the projector's compute buffer.

## Consequences
- Positive: image paste works end to end through LiteLLM/Open WebUI on all four slots; no context
  reductions were needed.
- Measured VRAM (stress: ~100-157K-token prompt plus a 1024x1024 image, 2026-10-23):
  - `coding` daily: idx1 peaks ~32.0 of 32.8 GB (~0.7 GB free, was ~1.8 GB before). `agentic` (204,800 ctx,
    P=2, MTP off): ~30.9 GB, fine.
  - `big`: 24.6 / 28.2 GB, plenty of room.
  - `small`: 9.3 of 12.3 GB.
  - `chat` with a **GPU** projector CRASHED (CUDA OOM, HTTP 502) on a 92K-token prompt plus an image: the
    slot already ran at ~31.2 of 32 GB at 96K ctx. Hence the CPU projector (peak 31.6 GB, 97K-token prompt
    plus image passed). CPU encoding is slower per image but costs no VRAM.
- Negative / trade-offs: `coding` daily has only ~0.7 GB of headroom on the longest prompts. If it ever
  OOMs, switch it to `--no-mmproj-offload` (as `chat`) or trim `--ctx-size`.
- Follow-ups: the Flash-Next/Strata work uses its own projector and is unaffected. Open WebUI re-sends
  all earlier turns (including web-search results) on every request, which inflates image chats (the test
  chat reached ~140K tokens); see the context-trimming discussion for options.

## Alternatives considered
- **GPU projector on `chat` with a smaller ctx:** would cost context on the MoE for a feature used rarely.
- **A separate vision slot:** an extra model on a V100 would evict `coding`/`chat`; rejected.

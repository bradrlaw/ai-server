# Architecture Decision Records (ADRs)

This directory tracks significant decisions for the headless AI server project,
using the [ADR](https://adr.github.io/) pattern (lightweight Nygard style).

## Why
Capture the *context* and *rationale* behind decisions so future work (and future
sessions) don't re-litigate settled choices or lose the reasoning behind them.

## Conventions
- One decision per file: `NNNN-short-title.md` (zero-padded, incrementing).
- Never delete an ADR. To reverse a decision, add a **new** ADR and set the old
  one's status to `Superseded by ADR-XXXX`.
- Statuses: `Proposed` -> `Accepted` -> (`Deprecated` | `Superseded`).
- Keep them short. Link to `../server-setup.md` for deep technical detail.

## Index
| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-keep-all-work-under-srv-ai.md) | Keep all work under `/srv/ai` | Accepted |
| [0002](0002-preserve-580-driver-decouple-toolkit.md) | Preserve nvidia-580-server driver; decouple CUDA toolkit | Accepted |
| [0003](0003-use-cuda-12.9-not-13.md) | Use CUDA 12.9 (not 13.x) for Pascal/Volta support | Accepted |
| [0004](0004-llama-cpp-primary-vllm-secondary.md) | llama.cpp as primary engine, vLLM secondary | Accepted |
| [0005](0005-gpu-workload-split.md) | GPU workload split (P100 aux, V100 primary) | Accepted |
| [0006](0006-containerize-inference-stack.md) | Containerize the inference stack | Accepted (hybrid) |
| [0007](0007-litellm-openai-api-gateway.md) | LiteLLM as OpenAI-style API gateway/router | Accepted |
| [0008](0008-primary-coding-model-qwen3.6-27b.md) | Primary coding model = Qwen3.6-27B dense (35B-A3B MoE alt) | Accepted |
| [0009](0009-gpu-shroud-fan-control.md) | GPU shroud fan control via nct6775 PWM + nvidia-smi temps | Accepted |
| [0010](0010-overall-serving-architecture.md) | Overall serving architecture (gateway + on-demand router + aux) | Accepted |
| [0011](0011-mcp-hosting-inventory.md) | MCP server hosting & inventory (mcpo) | Accepted |
| [0012](0012-comfyui-image-generation-mcp.md) | ComfyUI image-generation MCP tool (vendored comfyui-mcp-server) | Accepted |
| [0013](0013-dual-comfyui-instances.md) | Dual ComfyUI instances (open + password-locked), one per V100 | Accepted |
| [0014](0014-p100-fast-fp16-carveout.md) | Patch llama.cpp to disable FAST_FP16 on the Tesla P100 (sm_60) | Accepted |
| [0015](0015-llama-swap-serving-modes.md) | llama-swap serving modes (base + overlay) | Accepted |

## Template
Copy [`template.md`](template.md) for new records.

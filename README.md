# Headless AI Server

Configuration, operational scripts, and design docs for a personal headless AI
server (development + AI workloads for me and my family). The box serves local LLMs,
speech-to-text, embeddings/RAG, image generation, and long-running agents behind a
single OpenAI-compatible API.

> This repo tracks **docs** and **scripts** only. Large/local artifacts — models,
> Python venvs, source checkouts, datasets, and per-service data — live on the box
> under `/srv/ai/` and are intentionally git-ignored.

## Hardware

- **CPU/RAM:** Intel i7-6950X (MSI X99A), 128 GB RAM, 2 TB NVMe
- **GPUs:** 2× Tesla V100-32GB (sm_70) + 1× Tesla P100-16GB (sm_60), no NVLink (PCIe)
- **OS:** Ubuntu 24.04, NVIDIA driver 580-server, CUDA 12.x toolkit

## Layout

| Path | Tracked | Contents |
|------|---------|----------|
| `docs/` | ✅ | Runbook (`server-setup.md`), architecture (`architecture.md`), ADRs (`adr/`) |
| `scripts/` | ✅ | Setup, build, benchmark, and service scripts (fan/power control, CUDA, llama.cpp) |
| `models/`, `venvs/`, `src/`, `datasets/`, … | ❌ (local) | Model weights, virtualenvs, source builds, data |

## Documentation

- **[docs/server-setup.md](docs/server-setup.md)** — central runbook: hardware,
  CUDA/driver, llama.cpp build, benchmarks, fan/power control, GPU topology.
- **[docs/architecture.md](docs/architecture.md)** — serving architecture: topology,
  VRAM budget, model→backend routing, component matrix, phased rollout.
- **[docs/adr/](docs/adr/README.md)** — Architecture Decision Records (the *why*
  behind settled choices).

## Conventions

- All work lives under `/srv/ai` (ADR-0001).
- Significant decisions are recorded as ADRs (`docs/adr/`), never deleted — reversed
  by adding a new ADR.
- Privileged setup is delivered as scripts run with `sudo`; the fan/power daemon runs
  as a systemd service.

## Attribution

This server stands on the shoulders of many excellent open-source projects. None of
the projects below require attribution under their licenses, but they made this build
possible and deserve credit. (Model weights served by the stack are licensed
separately by their respective creators and are not redistributed here.)

### Core serving stack (native)

| Project | Link | How it's used |
|---------|------|---------------|
| llama.cpp | https://github.com/ggml-org/llama.cpp | GGUF LLM inference engine; built from source for the P100 (sm_60) and V100 (sm_70) GPUs. |
| llama-swap | https://github.com/mostlygeek/llama-swap | Model router / on-demand loader in front of llama.cpp; the matrix router picks co-resident model sets per GPU. |
| ComfyUI | https://github.com/comfyanonymous/ComfyUI | Node-based image/video generation server (run as two native instances: open + password-locked). |
| comfyui-mcp-server | https://github.com/joenorton/comfyui-mcp-server | Exposes ComfyUI style workflows as MCP tools (vendored, lightly patched). |

### Application tier (Docker Compose)

| Project | Link | How it's used |
|---------|------|---------------|
| LiteLLM | https://github.com/BerriAI/litellm | Client-facing OpenAI-compatible gateway that fronts llama-swap and cloud providers. |
| Open WebUI | https://github.com/open-webui/open-webui | Primary chat / RAG web front-end. |
| mcpo | https://github.com/open-webui/mcpo | MCP-to-OpenAPI proxy that surfaces MCP tool servers (e.g. ComfyUI) to Open WebUI. |
| SearXNG | https://github.com/searxng/searxng | Self-hosted metasearch engine used for web-search tooling. |
| Filebrowser | https://github.com/filebrowser/filebrowser | Web file manager for browsing/downloading generated assets and models. |

### ComfyUI custom nodes

| Project | Link | How it's used |
|---------|------|---------------|
| ComfyUI-Manager | https://github.com/ltdrdata/ComfyUI-Manager | Custom-node management and reproducible install snapshots. |
| ComfyUI-GGUF | https://github.com/city96/ComfyUI-GGUF | Loads GGUF-quantized diffusion models (e.g. Qwen-Image-Edit, FLUX.2) that don't fit as fp8 on the V100. |
| ComfyUI-KJNodes | https://github.com/kijai/ComfyUI-KJNodes | Utility nodes used across image/video workflows. |
| ComfyUI-WanVideoWrapper | https://github.com/kijai/ComfyUI-WanVideoWrapper | WAN text/image-to-video generation with block-swap and VAE tiling for the 32 GB V100. |
| ComfyUI-Frame-Interpolation | https://github.com/Fannovel16/ComfyUI-Frame-Interpolation | Frame interpolation for generated video. |
| rgthree-comfy | https://github.com/rgthree/rgthree-comfy | Quality-of-life nodes incl. the Power Lora Loader used by style workflows. |
| ComfyUI-Custom-Scripts | https://github.com/pythongosssss/ComfyUI-Custom-Scripts | Editor/UX enhancements for the ComfyUI graph. |
| ComfyUI-Easy-Use | https://github.com/yolain/ComfyUI-Easy-Use | Simplified/composite nodes for building workflows. |
| ComfyUI_Text_Translation | https://github.com/TFL-TFL/ComfyUI_Text_Translation | In-graph prompt translation. |
| ComfyUI-llama-cpp_vlm | https://github.com/mickeylan/ComfyUI-llama-cpp_vlm | Runs local VLM (GGUF + mmproj) inference inside ComfyUI for image captioning/analysis. |
| ComfyUI-Login | https://github.com/liusida/ComfyUI-Login | Password authentication for the locked ComfyUI instance. |
| llama-cpp-python (JamePeng fork) | https://github.com/JamePeng/llama-cpp-python | Python bindings backing the VLM node; built from source (v0.3.40) for sm_60/sm_70. |

### Tooling & infrastructure

| Project | Link | How it's used |
|---------|------|---------------|
| Hugging Face Hub / `hf` CLI + Xet | https://github.com/huggingface/huggingface_hub | Fast model downloads (Xet backend) into local model dirs. |
| NVIDIA Container Toolkit | https://github.com/NVIDIA/nvidia-container-toolkit | GPU access for the containerized app tier. |
| Tailscale | https://github.com/tailscale/tailscale | Private mesh network for remote access to the headless box. |
| GitHub Copilot CLI | https://github.com/github/copilot-cli | AI pair-programmer used throughout to build, debug, and document this server. |

## License

Released under the [MIT License](LICENSE).

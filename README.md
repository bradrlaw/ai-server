# Headless AI Server

Configuration, operational scripts, and design docs for a personal headless AI
server (development + AI workloads for me and my family). The box serves local LLMs,
speech-to-text, embeddings/RAG, image generation, and long-running agents behind a
single OpenAI-compatible API.

> This repo tracks **docs** and **scripts** only. Large/local artifacts — models,
> Python venvs, source checkouts, datasets, and per-service data — live on the box
> under `/srv/ai/` and are intentionally git-ignored.

## What it does

This is the full config + runbook for turning a single multi-GPU workstation into a
private, always-on AI appliance — one OpenAI-compatible endpoint that a household of
users (and their editors, chat apps, and agents) can point at instead of a paid cloud
API. The design goal is to squeeze modern models onto older, cheap datacenter GPUs
(Pascal/Volta, no NVLink) and keep them running cool, reliably, and unattended.

Capabilities:

- **Local LLM serving with automatic model swapping.** [llama.cpp](https://github.com/ggml-org/llama.cpp)
  behind [llama-swap](https://github.com/mostlygeek/llama-swap) loads models on demand
  and a *matrix router* picks co-resident model sets that fit across the three GPUs, so
  a coding model, a chat model, and a fast model can stay hot simultaneously and swap in
  heavier models on request — no manual juggling.
- **One OpenAI-compatible API for everything.** [LiteLLM](https://github.com/BerriAI/litellm)
  fronts the local router (and optional cloud fallbacks) so any OpenAI client — editors,
  scripts, [Open WebUI](https://github.com/open-webui/open-webui), IDE assistants — works
  unchanged. A helper script even points GitHub Copilot at the box (BYOK).
- **Image & video generation as callable tools.** Two [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
  instances (an open one and a password-locked one) run natively; curated style workflows
  (z-image, FLUX.1/FLUX.2, Krea-2, WAN video, song generation) are exposed as **MCP tools**
  so chat models and agents can generate media by name.
- **Local VLM, RAG & embeddings.** Vision-language inference runs inside ComfyUI; the app
  tier includes vector-DB/RAG plumbing and web search ([SearXNG](https://github.com/searxng/searxng)).
- **An agent/tool ecosystem.** [mcpo](https://github.com/open-webui/mcpo) surfaces MCP tool
  servers (time, fetch, git, ComfyUI, and a custom **plan-and-build** tool) to any client —
  including a "plan with one model, implement with another" GPU-aware coding workflow.
- **Thermal & power safety, unattended.** A custom daemon drives per-GPU shroud fans from
  memory temperature and applies self-healing power caps, keeping V100 HBM under its
  throttle point for longevity and quiet operation on a headless box.

## Hardware

- **CPU/RAM:** Intel i7-6950X (MSI X99A), 128 GB RAM, 2 TB NVMe
- **GPUs:** 2× Tesla V100-32GB (sm_70) + 1× Tesla P100-16GB (sm_60), no NVLink (PCIe)
- **OS:** Ubuntu 24.04, NVIDIA driver 580-server, CUDA 12.x toolkit

## Build cost

A rough bill of materials for the build. The goal was capable multi-GPU inference on
a tight budget by using previous-generation datacenter GPUs and a used base PC.

| Item | Cost |
|------|------|
| Used PC (motherboard, RAM, Intel SSD, Titan X GPU†, Rosewill 1200W PSU†) | $650 |
| Corsair HX1200i PSU (replaced the Rosewill) | $321 |
| NVIDIA Tesla V100 PCIe 32GB | $1,612 |
| NVIDIA Tesla P100 PCIe 16GB | $79 |
| Additional GPU power cable | $14 |
| Dual-GPU-to-single-connector power adapters | $30 |
| Fan ARGB extension cables | $8 |
| GPU fan shrouds | $54 |
| Arctic S4028-15K fans | $25 |
| Arctic S4028-6K fan | $9 |
| Rolling tower stand | $45 |
| GPU support bracket | $16 |
| Smart power plug | $23 |
| **Total** | **$2,886** |

† The Titan X GPU and Rosewill 1200W PSU came bundled with the used PC and are no
longer used.

> **Power usage & running costs:** to be added after real-world power measurement.

## Power usage

Measured with `nvidia-smi dmon`. These are **GPU-only** figures — whole-system draw at
the wall is higher (CPU, motherboard, NVMe, fans, and PSU conversion losses on top).

### Idle (models loaded, no active inference)

| GPU | Card | Power | Core temp | Mem temp |
|-----|------|-------|-----------|----------|
| 0 | Tesla P100-16GB | ~24 W | 38 °C | n/a¹ |
| 1 | Tesla V100-32GB | ~36 W | 42 °C | 41 °C |
| 2 | Tesla V100-32GB | ~38 W | 44 °C | 43 °C |
| **Total** | | **~98 W** | | |

¹ The P100 does not report an HBM memory-junction temperature.

The V100s idle at their memory clock (877 MHz) while the P100 clocks down to 405 MHz
core. Power caps applied at boot by the fan-control daemon keep the cards within thermal
limits (P100 200 W, V100 175 W each).

### Whole-system idle (at the wall)

| Metric | Value |
|--------|-------|
| Whole-system draw (models loaded, idle) | **170 W** |
| Electricity rate | $0.105 / kWh |
| Cost per day (24 h) | ~$0.43 |
| Cost per month (~730 h) | **~$13** (~124 kWh) |
| Cost per year | ~$156 (~1,489 kWh) |

The extra ~72 W over the ~98 W of GPUs is the CPU, motherboard, NVMe, fans, and PSU
conversion losses. Running costs under sustained inference load will be higher.

> **Load draw:** whole-system wall power and $/month under sustained inference to be
> added after measurement.

## Layout

| Path | Tracked | Contents |
|------|---------|----------|
| `docs/` | ✅ | Runbook (`server-setup.md`), architecture (`architecture.md`), ADRs (`adr/`) |
| `scripts/` | ✅ | Setup, build, benchmark, and service scripts (fan/power control, CUDA, llama.cpp) |
| `config/` | ✅ | llama-swap router config, ComfyUI MCP workflow definitions |
| `docker/` | ✅ | Compose stack for the CPU app tier (LiteLLM, Open WebUI, mcpo, SearXNG, plan-build MCP) |
| `models/`, `venvs/`, `src/`, `datasets/`, … | ❌ (local) | Model weights, virtualenvs, source builds, data |

## Notable components & scripts

Things others running older multi-GPU boxes may find reusable:

| Component | What it gives you |
|-----------|-------------------|
| `config/llama-swap.yaml` | Matrix router that auto-swaps **co-resident model sets** across 3 GPUs so daily models stay hot and heavy models swap in on demand. |
| `docker/mcpo/plan_build_mcp.py` | **Plan-and-build MCP tool** — plan a task with one model, implement it with another; GPU-aware so planner + coder stay co-resident (no swap). |
| `config/comfyui-mcp/workflows/*.json` + `scripts/comfyui-mcp-launch.py` | Turn ComfyUI style workflows into **MCP image/video/song tools** callable by name from chat/agents. |
| `scripts/install-comfyui-instances.sh` + `comfyui-*.service` | **Dual ComfyUI** setup — an open instance plus a password-locked one — from a single install, as systemd services. |
| `scripts/comfyui-free-gpu-node.py` | ComfyUI node that **unloads llama-swap LLMs and waits for VRAM** to actually free before a render, avoiding OOM on shared GPUs. |
| `scripts/comfyui-snapshot.sh` | **Reversible snapshots** of the ComfyUI custom-node/pip state so you can undo a bad node-pack install. |
| `scripts/gpu-fan-control.py` (+ `.service`, `.config.json`) | Temperature-driven **shroud-fan control + self-healing power caps** (drives off V100 HBM temp; re-caps GPUs that fall off/return on the bus). |
| `scripts/build-llama.sh` + `scripts/patches/p100-fast-fp16-carveout.patch` | Build llama.cpp for **sm_60/sm_70** with the P100 fp16-precision carveout applied. |
| `scripts/hf-dl` | Hugging Face downloads with the token **injected from the system keyring** (no plaintext token on disk). |
| `scripts/install-*.sh` | One-shot installers: CUDA 12.9, Docker + NVIDIA toolkit, llama-swap / ComfyUI / fan systemd services. |
| `scripts/bench-*.sh`, `power-cap-sweep.sh`, `verify-gpu-*.sh` | Benchmarking, power/thermal sweeps, and GPU/fan-mapping verification helpers. |
| `scripts/copilot-byok.sh` | Point **GitHub Copilot** at the local endpoint (bring-your-own-key). |

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
| iTerm2 | https://github.com/gnachman/iTerm2 | macOS terminal emulator used to drive SSH sessions and the Copilot CLI against the server. |
| iTerm2 `imgcat` | https://iterm2.com/documentation-images.html | Bundled in `scripts/imgcat` to preview generated images inline in the terminal over SSH. |
| P100 FAST_FP16 carveout (apollo-mg) | https://gist.github.com/apollo-mg/9218d50a209d70a85f033bf182657818 | 3-line llama.cpp patch disabling `FAST_FP16` on the P100 (sm_60) to stop fp16 accuracy drift; sourced from apollo-mg's write-up, merged in [llama-cpp-turboquant PR #212](https://github.com/TheTom/llama-cpp-turboquant/pull/212) ([TurboQuant+ tqp-v0.3.0](https://github.com/TheTom/llama-cpp-turboquant/releases/tag/tqp-v0.3.0)). See [docs/adr/0014](docs/adr/0014-p100-fast-fp16-carveout.md). |

## License

Released under the [MIT License](LICENSE).

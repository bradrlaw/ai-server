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

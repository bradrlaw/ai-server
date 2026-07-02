# ADR-0006: Containerize the inference stack

- **Status:** Accepted (hybrid — see 2026-07-02 addendum)
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
Different engines (llama.cpp, vLLM, TTS/STT) want different, sometimes conflicting,
CUDA/PyTorch/Python versions. Managing all of that natively on one host leads to
"dependency hell" and fragile upgrades. The 580 driver already supports everything
up to CUDA 13, so containers can pin any CUDA 12.x/13.x userspace independently.

## Decision (proposed)
Run inference engines in **Docker containers** using the **NVIDIA Container
Toolkit**, pinning CUDA/framework versions **per container**. Keep host CUDA (12.9)
for native builds/experiments, but treat containers as the reproducible runtime.
Compose files / Dockerfiles live under `/srv/ai/docker`.

## Consequences
- Positive: reproducible, isolated, independently upgradable engines; avoids host
  CUDA conflicts; easy rollback.
- Negative: image size/build time; a learning/ops overhead; GPU device plumbing.
- Follow-up: install Docker + NVIDIA Container Toolkit; validate a GPU container
  (`nvidia-smi` inside) before migrating engines. Promote to Accepted once proven.

## Alternatives considered
- **All native on host** - simplest short-term, but brittle across multiple
  engines/versions.
- **Python venv/conda per engine (no containers)** - isolates Python but not
  system CUDA libs; weaker reproducibility than containers.

## Addendum (2026-07-02): Accepted as a HYBRID, not full containerization

After weighing containers vs native for *this* hardware, the decision is a
**hybrid** split — because the two hard constraints here are (a) the GPUs are old
archs (**P100 sm_60, V100 sm_70**) that most prebuilt CUDA images no longer ship
kernels for → they'd need rebuilding anyway, negating containers' turnkey benefit
for GPU engines; and (b) modern Python ML wheels already bundle their own CUDA
runtime, so a **venv per engine** solves most "dependency hell" without Docker.
Containers' real, undiminished win is the **turnkey CPU/app tier** (official
images). GPU low-level services must stay native (need host `/sys`, `nvidia-smi`).

**Run-as mapping:**

| Layer | Run as | Rationale |
|-------|--------|-----------|
| Fan/power control, driver | **native systemd** | needs host `/sys` + `nvidia-smi`; already built (ADR-0009) |
| llama.cpp + **llama-swap** (V100/P100 LLM serving) | **native systemd** | already compiled for sm_60/sm_70; on-demand model swap needs host process control |
| vLLM / ComfyUI / faster-whisper / embeddings (GPU Python) | **native venv** (container only if a Volta/Pascal-capable image exists) | must build for old archs regardless; venv isolates torch cleanly |
| LiteLLM, Open WebUI, Qdrant, Postgres, n8n, Immich, Langfuse, Grafana | **containers (compose)** | maintained official images; CPU/DB-mostly, no GPU-arch issues |
| OpenClaw | **native systemd** (Node 24, already installed) | simplest; CPU daemon, no GPU |

**Consequences of the hybrid:** keeps the working native GPU stack; avoids fighting
Pascal/Volta inside images; still gets one-command deploys for the family-facing app
tier. Compose files under `/srv/ai/docker`; native services under `/srv/ai/scripts`
+ systemd. Docker + NVIDIA Container Toolkit still installed (some GPU Python engines
may be containerized case-by-case after arch-compatibility testing).

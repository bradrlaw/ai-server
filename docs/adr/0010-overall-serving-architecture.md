# ADR-0010: Overall serving architecture (gateway + on-demand router + aux services)

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The server must support a broad, growing set of workloads for the user and family:
coding assistants (VS Code + Copilot, Copilot CLI, opencode/pi), general chat with
web search, a message-from-anywhere / voice personal assistant, long-running
autonomous agents, real-time speech-to-text, a personal + published **image
library** (culling, style-based batch edits, generated metadata/descriptions),
image/video **generation**, and **RAG** over family documents.

Constraints already established by prior ADRs:
- Heterogeneous GPUs, no NVLink: **P100 16GB (sm_60)**, **2× V100 32GB (sm_70)**
  (ADR-0005: P100 aux, V100 primary; TP only for capacity, `-sm layer`).
- **llama.cpp primary**, vLLM secondary/V100-only (ADR-0004).
- **Hybrid deploy**: GPU/low-level native, CPU app tier in Docker (ADR-0006).
- Access is via **OpenAI-style API(s)** behind one gateway (ADR-0007).
- Cards run under power caps for longevity (ADR-0009): V100 175W, P100 200W.

Key requirements that shape the design: **on-demand model loading**, **multiple
models co-resident on one card when VRAM allows**, per-GPU placement, and an
**occasional large/high-quant model spanning both V100s** — which conflicts with
keeping two single-card models always resident.

## Decision
Adopt a layered architecture:

1. **Edge** — reverse proxy (Caddy/Traefik) for TLS + auth; Tailscale for remote.
2. **Gateway** — **LiteLLM** (ADR-0007): one OpenAI-compatible endpoint; model-name
   routing to backends; per-user/tool **virtual keys** with budgets (also enforces
   autonomous-agent guardrails); central logging/usage (→ Langfuse).
3. **LLM serving (native)** — a **model router (llama-swap)** in front of
   `llama-server` that owns the GPUs: loads models **on demand**, unloads on idle
   TTL, keeps multiple models **co-resident** where VRAM permits (P100 aux mix), and
   provides a **big-model TP=2 profile** that preempts (unloads) the two single-card
   V100 models to run one large/high-quant model across both V100s (`-sm layer`).
4. **Aux model services (native, P100)** — embeddings (TEI/Infinity), **faster-whisper**
   STT (real-time), a VLM captioner (image metadata), optional reranker.
5. **Generative media (native, burst)** — **ComfyUI** for image/video, scheduled as a
   burst job that grabs a **V100** when coding models are idle (not the P100 — Pascal
   is too weak for SDXL/Flux/video generation).
6. **Access surfaces** — **Open WebUI** (family browser chat + web search + light RAG)
   and **OpenClaw** (multi-channel + voice personal-assistant gateway, Node daemon);
   both consume LiteLLM.
7. **Data / RAG** — **Qdrant** (vectors) + Postgres (app state) + an ingestion
   pipeline (Docling/Unstructured) and a retrieval path (RAGFlow/R2R or LlamaIndex).
8. **Image library** — **Immich** (self-hosted photo library: CLIP semantic search +
   face recognition) paired with a captioning→**ExifTool** metadata-writeback pipeline.
9. **Orchestration / long-running agents & batch** — **n8n** (low-code workflows) and
   **Prefect** (Python image ETL/culling); autonomous agents call LiteLLM.
10. **Observability** — Langfuse (LLM traces) + Prometheus/Grafana + DCGM (GPU/thermal).

GPU/RAM allocation, VRAM budget, the model→backend routing table, tooling
alternatives, and phased rollout live in `../architecture.md` (the design doc).

## Consequences
- **Positive:** single stable OpenAI URL for every client; on-demand + co-resident
  models maximize the limited VRAM; clean per-card placement (ADR-0005); one big-model
  mode without dedicating hardware to it full-time; each capability has a clear owner.
- **Negative / trade-offs:** the gateway + router add hops and moving parts to run and
  monitor; the big-model TP=2 profile **briefly evicts** the two coding models; the
  P100 cannot do generative media, so ComfyUI competes with coding for a V100 during
  bursts; more services = more ops surface (mitigated by the hybrid split, ADR-0006).
- **Follow-ups / watch:** finalize the **model router** choice at build time
  (llama-swap primary; Ollama fallback) and give it its own ADR if it becomes load-
  bearing; validate faster-whisper real-time latency on the P100 (may need
  distil/medium); retest vLLM NCCL TP=2 under batched serving when vLLM is brought up;
  keep aux services within the P100's 16 GB budget.

## Alternatives considered
- **One always-on process per model, no router** — rejected: no on-demand loading, no
  co-residency, and no clean way to switch into the big-model TP mode; wastes VRAM.
- **Ollama as the primary router** — viable and simpler, but coarser GPU placement and
  it re-imports GGUFs via Modelfiles; kept as a fallback (see ADR-0004/0005).
- **vLLM as the primary engine** — rejected as primary: V100-only (Volta support is
  version-fragile) and P100 unsupported; no practical hot-swap of many models. Stays a
  secondary high-concurrency engine for a single pinned V100 model (ADR-0004).
- **Direct per-engine endpoints (no gateway)** — rejected (ADR-0007): no unified
  auth/routing, leaks topology to clients.
- **P100 for image/video generation** — rejected: Pascal lacks Tensor Cores / fast
  fp16; SDXL is slow and Flux/video infeasible. P100 stays on light aux; generation
  bursts on a V100.

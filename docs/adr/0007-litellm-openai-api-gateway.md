# ADR-0007: LiteLLM as OpenAI-style API gateway/router

- **Status:** Proposed
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The long-term goal is to access this server only via **OpenAI-style API(s)**.
Multiple backends will run concurrently (P100 embeddings/TTS/STT, V100 coding
models), each potentially exposing its own OpenAI-compatible endpoint. Clients
want a single, stable entry point, and the user wants "agent routing" (route a
request to the right model/backend).

## Decision (proposed)
Place **LiteLLM** in front of the engines as a unified **OpenAI-compatible
gateway/router**: one endpoint, model-name-based routing to the appropriate
backend (llama-server on V100s, aux services on P100), plus key-based auth and
usage tracking. Front with a reverse proxy; run services under systemd.

## Update (2026-07-02): Accepted
Promoted to **Accepted** as part of the overall architecture (ADR-0010). LiteLLM is
the single OpenAI-compatible gateway; all access surfaces (Open WebUI, OpenClaw,
coding tools, autonomous agents) consume it. Per-user/tool **virtual keys with
budgets** double as autonomous-agent guardrails; usage/traces flow to Langfuse. Runs
in the containerized app tier (ADR-0006 hybrid). Backends: llama-swap (V100/P100
LLMs), P100 aux services (embeddings/STT/rerank), optional vLLM. Routing table lives
in `../architecture.md`.

## Consequences
- Positive: single OpenAI-style URL for all clients; centralizes auth, routing,
  logging; matches the "agent routing" goal; backend swaps stay transparent.
- Negative: an extra hop/component to run and monitor; must secure the gateway.
- Follow-up: confirm LiteLLM covers needed endpoints (chat, embeddings, audio);
  define the model->backend routing table. Promote to Accepted once validated.

## Alternatives considered
- **Expose each engine's endpoint directly** - rejected; no unified auth/routing,
  leaks topology to clients.
- **Custom proxy** - rejected for now; unnecessary when LiteLLM exists.

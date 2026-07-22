# ADR-0016: Personal-assistant gateway layer (OpenClaw + Hermes)

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The stack already serves models (llama.cpp + llama-swap behind LiteLLM),
automations (n8n), coding (Copilot CLI + coding models), and media (ComfyUI),
plus in-house MCPs (plan-build, llama-swap-mode, ComfyUI). What was missing was a
**personal-assistant front door**: an always-on, multi-channel agent that holds
memory, chats over messaging platforms/voice, and delegates to the tiers above.

We evaluated three self-hosted frameworks:
- **OpenClaw** (`openclaw/openclaw`, MIT, Node 24) — multi-channel gateway (~23
  channels), Control UI, voice/Canvas, `models.json`/provider adapters, ClawHub.
  Polished "assistant gateway" ergonomics; first-class Docker image + LiteLLM
  provider.
- **Hermes** (`NousResearch/hermes-agent`, MIT, Python+Node) — agentic assistant
  with a **self-improving skill loop**, FTS5 session memory, subagent
  spawn/parallelize, 6 deploy backends, native GitHub Copilot ACP. More
  autonomous/experimental; official Docker Hub image, s6-supervised.
- **pi.dev** (`@earendil-works/pi-coding-agent`) — a terminal **coding** agent
  (a Copilot-CLI peer), **not** an assistant gateway. Out of scope for this layer.

Both assistant gateways are OpenAI-compatible, so they slot cleanly behind the
LiteLLM gateway (`:4000`). The constraint that shaped the deploy choice: both run
**agentic tool-execution loops** (Hermes even writes+runs its own skills), so
isolating that off the host matters.

## Decision
Adopt a **layered assistant architecture** and run **both** OpenClaw and Hermes
as the layer-1 assistant gateways (trial both; no need to pick a single winner
yet). Deploy **both as containers** in `docker/` (per the ADR-0006 hybrid model:
they are pure CPU app-tier, never touch a GPU, and containerization isolates their
tool-execution loops). Each points at LiteLLM `:4000` and delegates heavier work
to the existing tiers (n8n automations, Copilot CLI / coding models, ComfyUI +
plan-build + llama-swap-mode MCPs). Default model wiring: primary `chat`, fallback
`coding`, utility `fast` — the always-warm daily trio, so normal use needs no GPU
swap.

## Consequences
- Positive: one assistant front door over all channels; reuses the whole stack;
  tool-execution loops isolated in containers; both frameworks trialled side by
  side; convention-consistent with the hybrid app tier.
- Positive: no new gateway secrets in git — reuse `LITELLM_MASTER_KEY`; gateway
  auth tokens live in `docker/.env` (gitignored).
- Negative / trade-offs: two long-running assistant daemons to maintain; Hermes'
  self-improving skill loop executes code in-container (keep its `:8642` API
  LAN/firewalled; consider `terminal.backend: docker` for deeper sandboxing);
  Hermes is TUI-first (initial config via `docker ... setup` or pre-seeded files);
  OpenClaw's config schema is version-sensitive (`gateway.mode`/`bind`, LAN token,
  `controlUi.allowedOrigins`) — seed via `onboard --non-interactive`, repair with
  `openclaw doctor`.
- Follow-ups: wire messaging channels / voice; decide per-channel routing to
  `agentic` vs `daily` serving mode; evaluate which framework to standardize on
  after real use; add both to the status page + firewall allow-list.

## Alternatives considered
- **Pick only one (OpenClaw or Hermes)** — rejected for now: the two have
  different strengths (OpenClaw = channel breadth/polish; Hermes = autonomy/skill
  learning) and low marginal cost to run both containerized; defer the choice
  until real-world use.
- **pi.dev as the assistant** — rejected: it is a terminal coding agent (peer to
  Copilot CLI), not a multi-channel assistant gateway.
- **Run them native (systemd/npm + curl-install)** — rejected: violates the
  ADR-0006 hybrid convention for the CPU app tier and, more importantly, would run
  their agentic/skill-writing code loops directly on the host.
- **Build a bespoke gateway** — rejected: both mature MIT projects already cover
  channels, memory, and tool orchestration; not worth reinventing.

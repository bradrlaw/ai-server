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

## Addendum (2026-10-15) — the gateway adapts tool schemas to llama.cpp's grammar limits

**Context.** llama.cpp compiles tool-parameter and `response_format` JSON schemas into a
sampling grammar, and one incompatible keyword anywhere 400s the whole request:
- An unanchored `pattern` fails conversion (`Pattern must start with '^' and end with '$'`).
  JSON Schema patterns are search patterns and are normally unanchored (TypeBox, zod, pydantic).
- Large `maxLength`/`maxItems` bounds compile to repetitions that exceed the grammar parser's
  2000-rule limit (`Failed to initialize samplers: failed to parse grammar`).

OpenClaw's owner-only `cron` tool has both (`declarationKey` `pattern "\S"`, `trigger.script`
`maxLength 65536`). OpenClaw only sends it to admin-scoped clients such as the Control UI, so
every Control UI chat failed on the Qwen slots (`chat`, `coding`), while CLI-driven runs worked.

**Decision.** The LiteLLM pre-call hook (`docker/litellm/custom_hooks.py`) removes these
keywords before forwarding:
- any `pattern` not written `^...$`;
- `minLength`/`maxLength` on string types, and `minItems`/`maxItems`, of 1000 or more.

Measured cutoffs on build 9850: strings fail from `maxLength` 2000, and arrays with `minItems` 0
from `maxItems` 1999 (the optional wrapper's rules count toward the limit). 1000 leaves margin
for converter changes. Each distinct drop is logged once. Every client passes through the
gateway, so one fix covers OpenClaw, Hermes, Open WebUI tools, Copilot BYOK and pi.

**Consequences.**
- Positive: client tool schemas no longer have to suit llama.cpp. Verified live with the real
  `cron` schema: `chat`, `coding` and `small` return 200 via `:4000`, and `chat`/`coding` emit
  valid `cron` calls; the same request direct to `:9090` still 400s.
- Negative: the constrained grammar is looser than the client's schema wherever a keyword was
  dropped. Dropping beats the alternatives: anchoring changes a pattern's meaning, and clamping
  a bound would cut long arguments short. OpenClaw re-validates tool calls against its own
  schema (`validateToolArguments`), so a violating call becomes a tool error the model can
  retry. Direct `:9090` callers aren't covered. Every backend here is llama.cpp; gate the
  rewrite per deployment if a hosted model is ever added.
- Not handled (never seen in tool schemas): a large `{m,n}` quantifier, or nested repetitions,
  inside an anchored pattern.
- Rejected: patching llama.cpp (a local patch to carry through every rebuild); per-client fixes
  (each covers only one client).

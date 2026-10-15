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

## Addendum (2026-10-15) — Control UI remote access stays on the SSH tunnel

The OpenClaw Control UI needs a secure browser context (HTTPS or `localhost`) for its
WebCrypto device identity. Over plain HTTP from the LAN the page loads, but the gateway
then refuses the login (`1008 control ui requires device identity`). The owner's Mac hit
this at `http://aipcub.local:18789` on 2026-10-14: the SSH tunnel chosen on 2026-07-21
wasn't running, and the status page's `http://localhost:18789` link didn't say it needs
one.

Decision:

- **Keep the SSH tunnel** (`ssh -N -L 18789:127.0.0.1:18789 …`, then
  `http://localhost:18789`) as the way in from another machine. OpenClaw counts a
  token-authenticated `localhost` browser as container-local, so it pairs the browser
  without a manual approval. The owner's Mac browser has been paired since 2026-07-21.
- **The status page marks the link "SSH tunnel"**, with the exact tunnel command in its
  tooltip.

Rejected:

- **Tailscale Serve HTTPS** (`tailscale serve --https=443 http://127.0.0.1:18789`). It
  would work without an OpenClaw change: Serve keeps the `.ts.net` Host header, so the
  origin check passes, and each browser needs one `openclaw devices approve`. But
  enabling tailnet HTTPS certificates publishes the machine's tailnet DNS name in public
  Certificate Transparency logs, which are permanent. The owner declined.
- `gateway.controlUi.dangerouslyDisableDeviceAuth` — turns off the device-identity
  check for every client. A severe security downgrade.
- `gateway.controlUi.allowInsecureAuth` — still set in our config from the July setup,
  but it only applies to a loopback peer, and behind Docker's port mapping every
  browser arrives from the bridge IP. OpenClaw's own audit warns about it. Turning it
  off is a no-op cleanup, left to the owner.

Possible later, if tunnel-free or phone access is wanted:

- An HTTPS reverse proxy with a private CA. Private-CA certificates aren't publicly
  logged, but each device must trust that CA.
- On a desktop Chromium browser only, the per-browser flag
  `unsafely-treat-insecure-origin-as-secure` for the server's URL. This sends the token
  over plain HTTP unless the URL is the Tailscale address.

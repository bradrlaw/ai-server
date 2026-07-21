# ADR-0015: llama-swap serving modes (base + overlay)

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context

The single `config/llama-swap.yaml` is tuned for *balanced daily use*: every
model runs `--parallel 1` (coder-next `2`) so each request keeps the maximum
possible context and VRAM headroom stays comfortable. But different workloads
want different trade-offs:

- **Heavy coding** — a human-driven interactive coding session that spawns
  several sub-agents. The primary model should keep its full context; the
  sub-agent workers should have *several parallel slots* so delegated tasks run
  concurrently instead of serializing behind one slot.
- **Autonomous agentic** (future) — pure unattended fan-out where per-request
  context matters less than aggregate throughput.

The `--parallel` throughput sweep (2026-07-21, see `docs/benchmarking.md`)
quantified the trade-off: raising `--parallel` gives ~2.6–2.9× aggregate
throughput at the ceiling, but splits `--ctx-size` across slots (e.g. `chat` at
P=8 = 16.4k/slot) and OOMs VRAM-tight cards. So there is no single "best"
setting — it depends on the workload. We want to switch between named
configurations easily, including from a client, without hand-editing the
heavily-commented YAML or restarting the service.

Constraints: agents/clients cannot `sudo`; llama-swap already runs with
`-watch-config` (reloads the YAML on change); the config's inline comments carry
essential tuning rationale and must not be duplicated per mode.

## Decision

Introduce **serving modes** as a *base + overlay* system:

- `config/llama-swap.base.yaml` — the canonical, fully-commented config (the old
  `llama-swap.yaml`, renamed). Single source of truth for model blocks + matrix
  routing. **Tracked in git.**
- `config/modes/<mode>.yaml` — small declarative overlays: per-model `overrides`
  (`parallel`, `concurrencyLimit`, optional `ctx_size`), a boot `preload` list,
  and an optional switch-time `warm` list. **Tracked in git.**
- `config/llama-swap.yaml` — the **rendered active file** the service reads. It
  is *generated* (base + overlay + an `# ACTIVE-MODE:` marker), so it is
  **gitignored** (runtime state, regenerated on demand — no churn).
- `scripts/llama-swap-mode.py` — the renderer/switcher: `list` / `current` /
  `show [mode]` / `set <mode>` (`--json` for machines). `set` writes the active
  file, lets `-watch-config` reload, and warms the mode's models. No sudo/restart.
- `docker/mcpo/llama_swap_mode_mcp.py` — an MCP server (host streamable-http on
  :9120, `scripts/llama-swap-mode-mcp.service`, proxied by mcpo) exposing
  `list_modes` / `current_mode` / `show_mode` / `set_mode` so Open WebUI and
  Copilot BYOK clients can switch modes.
- The status page (`scripts/server-status-service.py`) shows the active mode and
  each loaded model's `--parallel` + context-per-slot.

Modes shipped initially:

- **daily** — base verbatim (all single-slot; coder-next `2`). Boot-preloads
  `fast`; a switch back warms the daily trio (coding + chat + fast).
- **heavy-coding** — `coding` stays `--parallel 1` (full 200k ctx, interactive
  primary on idx1); `chat` (idx2) and `fast` (idx0) go to `--parallel 4` as
  sub-agent worker pools (32.8k ctx/slot; `concurrencyLimit 12`). Same card
  residency as daily, so no eviction conflicts. Verified live: chat + fast each
  came up with 4 KV slots @ 32k, coding 29.5 GB / chat 30.3 GB / P100 13.2 GB —
  no OOM.
- **agentic** — autonomous multi-agent throughput. Loads `gemma-26b` on idx2
  **instead of `chat`** (they share idx2; matrix set `qg` = `coding & gemma-26b &
  fast`). `coding` (idx1) `--parallel 2` (~100k ctx/slot) for concurrent automated
  coding; `gemma-26b` (idx2) `--parallel 8` (~16k ctx/slot, ~281 tok/s aggregate)
  to blitz many small-context agent tasks; `fast` (P100 idx0) `--parallel 2`
  (~64k ctx/slot). `concurrencyLimit` raised above `--parallel` (8/16/8) so bursts
  queue instead of 429ing. Verified live 2026-07-21: coding 28.7 GB / gemma-26b
  21.5 GB / P100 fast 11.1 GB — all co-resident, no OOM.

## Consequences

- Positive: one-command / one-MCP-call mode switching with no restart; the
  base config stays the single documented source; the active file is
  self-describing (`# ACTIVE-MODE:` marker) and read by both the switcher and
  the status page; sub-agent binding stays a *client* concern (e.g. Copilot SDK
  `customAgents` pinning sub-agents to `chat`/`fast`) — the mode just makes the
  server ready.
- Negative / trade-offs: `config/llama-swap.yaml` is now generated, so it must
  be produced by `llama-swap-mode.py set <mode>` on a fresh deploy (the old file
  was hand-tracked). Editing tuning now happens in `llama-swap.base.yaml`, not
  the active file (a hand-edit to the active file is overwritten on the next
  switch). The MCP endpoint is unauthenticated (LAN/Tailscale only), like
  plan-build / comfyui-mcp.
- Follow-ups: the **agentic** mode (autonomous multi-agent throughput) has been
  added (see Modes above). Consider a matching LiteLLM view if a mode ever exposes
  new model names (none so far do — `agentic` swaps `chat`→`gemma-26b`, both
  already in `docker/litellm/config.yaml`, and heavy-coding just adds slots).

## Alternatives considered

- **Separate full YAML per mode** — simplest to reason about, but duplicates all
  11 heavily-commented model blocks 3× and drifts on every base edit. Rejected
  for the maintenance burden.
- **Keep one config, edit `--parallel` by hand per session** — error-prone, not
  client-switchable, and loses the "what's active" marker. Rejected.
- **LiteLLM-only routing (`--parallel` unchanged)** — LiteLLM can fan out
  requests, but the real concurrency ceiling is llama-swap's per-model
  `--parallel` slot count + `concurrencyLimit`; the mode must change *those* on
  the engine. Rejected as insufficient.

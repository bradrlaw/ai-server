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
- **heavy-coding** — `coding` stays `--parallel 1` (full 180k MTP ctx, interactive
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

## Addendum (2026-07-23) — MTP self-speculative decode on `coding` + `chat`

Both single-slot daily models now serve the MTP-equipped GGUF variants with
`--spec-type draft-mtp --spec-draft-n-max 2` (see docs/benchmarking.md and
ADR-0008). This changes the single-slot contexts referenced above:

- **daily / heavy-coding `coding`** — Qwen3.6-27B-MTP Q6_K, ctx **184320 (180k)**,
  not 200k (MTP's extra ~1 GB compute buffer OOMs at 200k). +79% decode.
- **daily `chat`** — Qwen3.6-35B-A3B-MTP UD-Q6_K, ctx **98304 (96k)**, not 128k. +31% decode.

Mode overlays gained `spec` and `model` override keys in `llama-swap-mode.py`
*(the two `model:` overrides below were removed 2026-10-15 — see that addendum)*:

- **heavy-coding `chat`** overrides back to the **non-MTP** file at `--parallel 4`
  / ctx 131072 / spec off — MTP is a single-stream latency win, wasted (and VRAM-costly)
  on a batched sub-agent pool.
- **agentic `coding`** overrides back to the **non-MTP** file at `--parallel 2` /
  ctx 204800 / spec off for the same reason.

So MTP is active only where a slot serves one stream at a time.

## Addendum (2026-10-15) — pools inherit the base model file (no `model:` overrides)

The two `model:` overrides above pinned the pools to non-MTP copies that later moved to the
HDD cold tier (`models/cold` → `/srv/ai/storage-bulk`), and the 2026-08-15 Qwen3.8 migration
of the base `coding` slot didn't touch them:

- **agentic `coding`** still served the pre-3.8 **Qwen3.6-27B** — a stale model.
- **heavy-coding `chat`** loaded its 29.3 GB file from the HDD, so a switch into the mode
  took **3 min 1 s** (chat alone 2 min 31 s).

Decision: the overlays drop `model:` and keep `spec: none`, so each pool loads the same
NVMe file as its base slot, with the embedded MTP head loaded but unused. The base stays the
single source of *which model* a slot serves, so a future migration can't miss a mode. The
`model` override key remains in `llama-swap-mode.py`; no shipped overlay uses it.

MTP stays **off** for both pools because, on this build, it doesn't scale with `--parallel`
(aggregate tok/s by number of busy slots):

| | MTP on (2026-08-24 sweep) | MTP off (2026-07-21 sweep) |
|---|---|---|
| `chat` P=4 — 1 / 2 / 4 busy | 125 / 115 / 124 | 85 / 123 / 153 |
| `coding` P=2 — 1 / 2 busy | 34 / 32 | 22 / 37 |

(MTP-off `coding` was Qwen3.6-27B, the same architecture as 3.8.) MTP only wins while a
single slot is busy, so it stays on for the single-slot primaries (daily `coding`/`chat`,
heavy-coding `coding`).

Measured 2026-10-15 — every slot filled to its full context at once, peak VRAM sampled
every 200 ms; "free" = 32768 − 274 MiB driver reserve − peak (idx1 also holds
comfyui-open's ~0.8 GB idle context):

- heavy-coding `chat` P=4 / 131072: 31180 MiB idle → **31350 MiB peak, 1144 MiB free**
  (the old non-MTP file peaked at 30680 MiB — the unused head costs ~0.7 GB).
- agentic `coding` (Qwen3.8 Q6_K) P=2 / 204800: 30578 MiB idle → **30814 MiB peak,
  1680 MiB free**. Single-stream decode 22.4 tok/s (MTP off), as expected.
- Switch time with the chat file uncached on NVMe: heavy-coding **62 s** (chat 33 s);
  agentic **34 s**.

## Addendum (2026-10-15) — `small` pools get 32k per slot (audit C9)

heavy-coding and agentic ran `small` at ctx 24576 @ P=2, i.e. **12,288 tokens per
request**. That cap was sized for the 26B MoE (~15.3 GB) on the 16 GB P100. Since the P100
died, `small` serves the dense Gemma-4-12B on the 12 GB Titan X stopgap (ADR-0017), and
every client assumes daily's 32k window:

- OpenClaw/pi `contextWindow` is 32768.
- Copilot BYOK budgets a 20480-token prompt plus 8192 output.

The mismatch surfaced once Open WebUI's Small regained its tools (moved from the orphaned
`fast` entry during C10). Those tools come to ~14k tokens:

- ComfyUI-MCP: 22 tools, ~7.7k tokens.
- plan-build: ~3.1k tokens.
- Server Mode: ~0.5k tokens.
- Open WebUI's built-in tools on top.

So `fast_make_plan` in heavy-coding failed with *"request (14086 tokens) exceeds the
available context size (12288 tokens)"*.

Decision (owner): both overlays now run `small` at **ctx 65536 @ P=2 = 32k per slot**,
the same per-request window as daily. The dense 12B can afford it because only its 8
global-attention layers (1 KV head, 512-dim K/V) grow with context. The measured cost is
+487 MiB for 40,960 more tokens, ~12 KiB/token. Its 40 sliding-window layers stay at the
1024-token window.

Measured 2026-10-15 in heavy-coding. agentic renders an identical `small` block, and idx0
holds only `small` in both modes, so the result covers both:

- Idle VRAM: 9588 MiB → **10075 MiB**.
- Both slots filled with 32,704-token prompts at once: **10308 MiB peak of 12288, ~1.9 GB
  free** (after the 90 MiB driver reserve).
- The mode switch reloaded all three models in 30 s.
- The owner's case now succeeds: a 13,858-token request via LiteLLM returns 200.
- Prefill on the Maxwell card is slow. A cold ~14k-token prompt takes **~113 s**
  (~120 tok/s). With both slots busy, each gets 86–102 tok/s. Follow-ups that reuse the
  cached prefix answer in ~1 s.

If `small` returns to the 26B MoE on a 16 GB card, drop both overlays back to 24576. The MoE
OOMed at 48k+ even at P=1. Both overlays and the base's TEMPORARY block say so.

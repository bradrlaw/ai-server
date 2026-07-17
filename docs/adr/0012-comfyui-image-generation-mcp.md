# ADR-0012: ComfyUI image-generation MCP tool (vendored comfyui-mcp-server)

- **Status:** Accepted
- **Date:** 2026-07-05
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
Open WebUI's native ComfyUI integration stores exactly **one** generation workflow +
**one** edit workflow globally, and its image "Model" dropdown only swaps the *checkpoint
filename* within that single graph — it cannot select between different workflow
topologies (e.g. Z-Image Turbo vs Flux vs a LoRA-stacked style). We want:
- **Multiple style workflows** selectable per request without swapping OWUI config.
- **Agent/long-running use** — image gen callable from any MCP client (OWUI chats,
  Copilot CLI, future OpenClaw), including from long-running processes, with async jobs.

The official first-party Comfy MCP (shipped 2026-06-30) is **cloud-only**
(`cloud.comfy.org/mcp`, needs a subscription); its Partner MCP / Comfy CLI are API-based
with **no custom workflows**. None drive a local ComfyUI with custom graphs. Comfy's own
docs point to community projects for local use.

## Decision
Adopt the community **[joenorton/comfyui-mcp-server](https://github.com/joenorton/comfyui-mcp-server)**
(Python, FastMCP) as an image/media MCP tool, integrated as follows:

- **Vendored clone** at `/srv/ai/src/comfyui-mcp-server` (gitignored) so it can be updated
  with `git pull`. Kept **pristine** except a few local commits (genuine bug/UX fixes — see
  Consequences), candidates to upstream. Because the clone is gitignored, those commits are
  exported to **`scripts/patches/comfyui-mcp-server-local.patch`** (a `git format-patch` mbox
  over upstream `e0101b2`) so a re-clone is reproducible: `git clone … && git checkout e0101b2
  && git am /srv/ai/scripts/patches/comfyui-mcp-server-local.patch`.
- **Native systemd service** `comfyui-mcp.service` (CPU-only bridge, venv
  `/srv/ai/venvs/comfyui-mcp`), streamable-http on `0.0.0.0:9000`, talking to native
  ComfyUI at `127.0.0.1:8188`. A small tracked launcher
  `scripts/comfyui-mcp-launch.py` overrides the FastMCP bind host (upstream hard-codes
  127.0.0.1) and relaxes DNS-rebinding host checks so the mcpo container can reach it —
  without forking upstream.
- **Style workflow library** in `config/comfyui-mcp/workflows/` — **tracked in this repo**
  (config/ whitelist), NOT in the clone, so styles are version-controlled and survive
  re-cloning. Each `*.json` with `PARAM_<TYPE>_<NAME>` placeholders auto-registers its own
  MCP tool (e.g. `z_image_turbo`), with defaults from an optional `*.meta.json` sidecar.
- **Exposed via mcpo** (ADR-0011) as a `streamable-http` proxy entry `comfyui` in
  `docker/mcpo/config.json`, so Open WebUI and agents see the tools alongside the rest.
- **GPU coordination unchanged:** the tool calls ComfyUI's `/prompt`, which fires the
  existing `free_gpu` hook (per-model unload keeping chat+fast, idle watchdog) — so VRAM
  management works regardless of caller.

First style shipped: **Z-Image Turbo** (`z_image_turbo`), 4-step Lumina2/AuraFlow,
1024², cfg baked at 1.

## Consequences
- Positive: multiple styles via one tool each (`z_image_turbo`, …) + generic
  `run_workflow`; async jobs (`get_job`/`cancel_job`), iterative `regenerate`, inline
  `view_image`; usable by **any** MCP client, not just OWUI; no OWUI global-config swaps.
- Positive: styles are versioned in-repo; upstream stays updatable via `git pull`.
- Negative / trade-offs: a community dependency (audited: deps = requests/mcp/Pillow, no
  exec/subprocess/external hosts; pinned at upstream `e0101b2`). Auth-less on the LAN like
  ComfyUI. The local commits mean `git pull` may need `--rebase`.
- Local patches (exported to `scripts/patches/comfyui-mcp-server-local.patch`): (1) upstream
  `_load_workflows` did not skip `.meta.json` sidecars (unlike `get_workflow_catalog`),
  crashing startup when a sidecar exists; (2) mcpo serializes MCP `ImageContent` to an inert
  data-URI string OWUI won't render, so responses now carry a browser-reachable `markdown`
  image link (env `COMFY_MCP_RETURN_MARKDOWN` / `COMFY_MCP_PUBLIC_URL`), plus tool-description
  tweaks so models emit the exact reachable URL instead of a placeholder host.
  All fixed locally in the clone — candidate PRs upstream.
- Follow-ups: add more styles (Flux, SDXL, LoRA stacks) as workflow files; consider a thin
  OWUI Pipe later if native-picker UX is wanted. Inline display uses the `markdown` field
  (`![id](http://<server-LAN-ip>:8188/view?filename=…)`) which the model echoes; the LAN/Tailscale
  URL must match how the browser reaches the host (set via `COMFY_MCP_PUBLIC_URL`).

## Alternatives considered
- **Build our own MCP server** — rejected: joenorton already provides the workflow-library
  + async-job design we wanted; only glue was needed.
- **OWUI Functions (Pipe), one per style** — viable and native-feeling, but OWUI-only (not
  usable by other agents / long-running processes) and more OWUI-specific code.
- **Hot-swap OWUI's global image config per request** — rejected: global mutable state,
  race conditions under concurrent multi-user generation.
- **Same-graph checkpoint swap via OWUI image Model dropdown** — rejected: only varies the
  checkpoint file, not the graph/sampler/steps.
- **Official Comfy Cloud / Partner MCP** — rejected: cloud-only or no custom local
  workflows; we have local V100s and custom graphs.
- **mcpo stdio subprocess (like plan-build)** — viable, but the server prints banners near
  stdout and needs its multi-file code + deps inside the mcpo container; the native
  streamable-http service is independently testable (curl) and matches the GPU-adjacent
  native tier.

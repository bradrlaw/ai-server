# ADR-0025: aichat terminal client with comfyui-secure MCP tools

- **Status:** Accepted
- **Date:** 2026-10-11
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The owner wanted a lightweight, terminal-native client to talk to the server's
models over the LiteLLM gateway (`:4000`) while sitting on the box over SSH — with
model selection and, ideally, the ability to generate images (via the login-gated
**comfyui-secure** instance) without opening Open WebUI in a browser.

Constraints: agents cannot `sudo`/`brew`/`cargo`; the client had to install from a
static binary into `~/.local/bin`. It must **not** disturb the comfyui-secure jobs
on GPU idx2, and must **not** modify the shared MCP/mcpo path that Open WebUI uses
(that path wraps comfyui-secure as typed OpenAPI through `mcpo`).

## Decision
Install **`aichat`** (sigoden, MIT) as the terminal client, configured against the
LiteLLM gateway, listing every model it serves. Wire in the comfyui-secure MCP tools via
**`llm-functions`** + **`argc`** (both sigoden, MIT), running the `llm-functions`
**MCP bridge** as a boot-persistent systemd service that re-exposes the tools for
aichat's function-calling.

Key choices and fixes:
- **Default model** = `chat-uncensored-q6`; **default image tool** = the
  self-contained local `comfyui_secure_krea2_turbo_bf16` workflow, steered by a
  default role applied via `cmd_prelude`/`repl_prelude`.
- **Patched the `llm-functions` bridge** (MIT, changes also MIT) to (a) speak
  streamable-http/SSE so it can reach comfyui-secure's MCP at `:9001/mcp`, and
  (b) repair/sanitize the comfyui-secure schema (typeless numeric params) so
  aichat stops looping on `{"width":{"width":1024}}`. Open WebUI's mcpo path is
  untouched. See `config/aichat/llm-functions-bridge.patch`.
- **Tools are activated by the role's frontmatter** (`use_tools: comfyui`), not a
  top-level config key — aichat ignores a top-level `use_tools`. Each tool-using
  model also carries `supports_function_calling: true`. Without BOTH, aichat sends
  no tools and the model hallucinates a textual `<tool_call>` plus a **fake result
  URL** (observed: a bogus `krea-production-public.s3.amazonaws.com` link) while
  generating nothing — a privacy-alarming but purely local hallucination.
- **No per-tool confirmation prompt**: `tools/llm-functions/.env` sets
  `LLM_MCP_SKIP_CONFIRM=comfyui_secure_[a-z0-9_]+` so the comfyui tools run without
  the `[Y/n]` prompt; other MCP servers added later still prompt.
- **Logging off**: aichat `save: false` (no transcript at `~/.config/aichat/messages.md`).

The whole client is **local & gitignored** (`~/.local/bin/{aichat,argc}`,
`~/.config/aichat/`, `/srv/ai/tools/llm-functions/`). The repo tracks a reproducible
snapshot under `config/aichat/` (sanitized config, role, mcp.json, the bridge patch,
attribution README) plus the systemd unit + installer under `scripts/`.

## Consequences
- Positive: image generation and multi-model chat from a bare SSH shell; local-only
  (nothing leaves the box); OWUI/mcpo path untouched; reproducible from the tracked
  snapshot + upstream `616d8d8`.
- Negative / trade-offs: the client lives outside the repo (local dirs), so the
  snapshot must be kept in sync by hand after config changes. The bridge opens a
  single session to its upstream MCP and has **no reconnect logic**, so it must be
  restarted whenever `comfyui-mcp-secure` (`:9001`) restarts; this is now handled
  automatically by `PartOf=comfyui-mcp-secure.service` on the bridge unit (see
  below). The comfyui schema patch is name-heuristic; new typeless params may need
  the hint lists extended.
- Follow-ups / things to watch: keep the `config/aichat/` snapshot updated when the
  roster or tool set changes. Note the restart hook is deliberately tied to
  `comfyui-mcp-secure` (the bridge's actual upstream), **not** `comfyui-secure`
  (`:8189`): quiet hours stops/starts the latter, and `PartOf` doesn't propagate
  *start*, so tying to it would strand the bridge down for the rest of the day.
- **Update (2026-10-13):** upstream's `app.listen(PORT)` left the bridge reachable
  from the LAN (`:8808/tools` answered), bypassing the secure canvas's login. It is
  now loopback-only. A fourth patch item binds `MCP_BRIDGE_HOST`, default
  `127.0.0.1`, and the unit kernel-filters to localhost. See
  [ADR-0026](0026-secure-mcp-bridge-network-lockdown.md).

### Restart hook
The bridge unit declares `After=comfyui-mcp-secure.service` + `PartOf=comfyui-mcp-secure.service`.
`PartOf` propagates the upstream MCP's stop/restart to the bridge, and `After`
ensures the bridge comes back up only once the upstream is ready — so the one event
that breaks the bridge's session (`systemctl restart comfyui-mcp-secure`, e.g. after
a workflow-param change) now transparently restarts the bridge too. Quiet hours does
not touch `comfyui-mcp-secure`, so the daily deep-idle cycle leaves the bridge alone.

## Alternatives considered
- **Open WebUI only** — already available, but browser-bound; the ask was
  explicitly a terminal client on the server.
- **Native MCP in the client** — aichat has no native MCP; `llm-functions` + the
  bridge is the supported path and reuses the existing comfyui-secure MCP.
- **Point aichat at mcpo's OpenAPI** (the OWUI path) — would couple the CLI to the
  shared OWUI tool surface and risk perturbing it; kept the paths independent.
- **A more reliable tool-calling model as default** (e.g. `coding`) — rejected; the
  owner wanted the uncensored chat slot as default, and once tools are actually
  attached the uncensored model emits correct native tool calls.

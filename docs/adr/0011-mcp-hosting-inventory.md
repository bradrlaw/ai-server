# ADR-0011: MCP server hosting & inventory (mcpo)

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The stack increasingly relies on **MCP (Model Context Protocol)** servers to give
models/agents tools (time, memory, filesystem, web, custom family/data tools). Two
questions arose: **where do MCP servers run**, and **how do we manage/inventory them**?

Consumers of MCP tools on this server split into two kinds:
- **OpenAPI/HTTP tool consumers** — Open WebUI, LiteLLM-fronted agents, n8n. These do
  not speak raw MCP stdio.
- **Native MCP clients** — coding harnesses (this Copilot CLI, opencode, Claude Code)
  that already speak MCP over stdio directly.

Raw MCP stdio is insecure, hard to inventory, and incompatible with HTTP tools.

## Decision
Host MCP servers **on this machine** (CPU app tier), and manage them centrally with
**[`mcpo`](https://github.com/open-webui/mcpo)** — Open WebUI's official
**MCP-to-OpenAPI proxy** — running as a container in the app-tier compose stack.

- **Single versioned inventory:** `docker/mcpo/config.json` (Claude-Desktop
  `mcpServers` format) is the **source of truth** for what MCP servers exist. It is
  tracked in git. Adding/removing a server = edit this file.
- **Hot reload:** mcpo runs with `--hot-reload`, so inventory edits apply without
  downtime.
- **Exposure:** each MCP server becomes an authenticated OpenAPI route
  (`http://mcpo:8000/<name>` on the `ai` network; `127.0.0.1:8000` for debug) with
  auto-generated docs at `/<name>/docs`. Auth via `MCPO_API_KEY` (in `.env`).
- **Dual consumption:** OpenAPI consumers (Open WebUI, etc.) use mcpo over HTTP;
  native MCP clients (coding harnesses) may point at the same MCP servers directly
  over stdio — mcpo is only required for the HTTP/OpenAPI side.

## Consequences
- One file (`mcpo/config.json`) is the authoritative, reviewable MCP inventory.
- HTTP consumers get security (bearer auth), docs, and stability for free.
- **Runtime caveat:** the `mcpo:main` image ships `uv`/`uvx` (Python MCP servers work
  out of the box, e.g. `mcp-server-time`). Node-based servers (`npx`) need a
  node-enabled image or a sidecar; adopt as needed.
- Secrets for individual MCP servers (API keys, tokens) go in `.env` / the config and
  are never committed.
- Open WebUI registers mcpo tool routes under Settings → Tools (External / OpenAPI
  tool servers) using the mcpo base URL + `MCPO_API_KEY`.

## Alternatives considered
- **Native MCP stdio everywhere** — rejected: insecure, no HTTP interop, no central
  inventory, per-client wiring.
- **Ad-hoc per-tool HTTP servers** — rejected: reinvents what mcpo standardizes
  (auth, OpenAPI docs, hot-reload, one config).

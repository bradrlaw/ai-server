# ADR-0034: Headless-browser fetch MCP (`browser-fetch`) for Reddit and other fetch-hostile sites

- **Status:** Accepted (code + tracked config done; the owner must run the privileged install steps below)
- **Date:** 2026-10-26
- **Deciders:** @bradrlaw (+ Copilot CLI)
- **Related:** [ADR-0028](0028-host-firewall-ufw.md) (firewall allowlist), [ADR-0026](0026-secure-mcp-bridge-network-lockdown.md) (MCP lockdown)

## Context
Reddit answers plain HTTP clients from this server with 403 "blocked by network policy" (curl, `.json`, old.reddit,
`.rss`, mcp-server-fetch, r.jina.ai), even though the same public IP browses fine from another machine, so the block is
client-fingerprint based, not IP based. A real headless Chromium (Playwright) gets the full page. SearXNG finds Reddit
URLs but cannot read them; PullPush (a Pushshift mirror) reads threads but rate-limits and lags.

## Decision
- New native MCP server `docker/mcpo/browser_fetch_mcp.py` (tools `browser_fetch`, `reddit_search`), served over
  streamable-http on `0.0.0.0:9130/mcp` by `scripts/browser-fetch-mcp.service` (installer:
  `scripts/install-browser-fetch-mcp-service.sh`). Runs on the host in its own venv `venvs/browser`
  (Playwright, `mcp<2`, Chromium in `venvs/browser/pw`; all gitignored). CLI twin: `scripts/browser-fetch.py`.
- Port `9130` added to the container->host allowlist (`DOCKER_HOST_PORTS` in `setup-firewall.sh` and `verify-firewall.sh`).
- Clients: mcpo entry `browser-fetch` (Open WebUI / agents) and `mcp_servers.browser-fetch` in the live
  `hermes/config.yaml` (local, gitignored). The two Hermes scout skills now tell the agent to use it for Reddit.
- Safety: no auth (like the other bridges, LAN/Tailscale only); every browser request is refused unless the host resolves
  only to public IPs (no loopback/LAN/Docker/link-local, no `file:`), concurrency 2, `MemoryMax=3G`.

## Verified (2026-10-26)
Thread and search pages returned in full (23 comments); loopback, LAN and `file://` URLs were refused.

## Owner steps (need sudo)
1. `sudo /srv/ai/scripts/install-browser-fetch-mcp-service.sh`
2. `sudo /srv/ai/scripts/setup-firewall.sh` (adds 9130), then `scripts/verify-firewall.sh`
3. `cd /srv/ai/docker && docker compose restart mcpo hermes` (mcpo has no hot reload; Hermes reads `mcp_servers` at start)

## Consequences / watch
- Scraping Reddit may break when its markup or bot detection changes; the tools return text, so the agent copes with layout drift.
- Each call is slow (5-15 s) and uses a few hundred MB of RAM. Not for bulk crawling; respect Reddit's terms.

# ADR-0026: Lock the secure ComfyUI MCP bridges to localhost + mcpo

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The secure ComfyUI canvas (`comfyui-secure`, `:8189`, ADR-0013) is gated by
ComfyUI-Login. Two **auth-less** bridges sit in front of it and carry that
credential:

- **`comfyui-mcp-secure`** (`:9001`) — the vendored comfyui-mcp-server pointed at
  `:8189`. It injects the ComfyUI-Login bearer token (read from
  `comfyui/login/PASSWORD`) into every ComfyUI call. It bound `0.0.0.0` so mcpo
  could reach it via `host.docker.internal`.
- **`aichat-mcp-bridge`** (`:8808`, ADR-0025) — the llm-functions bridge. Upstream
  calls `app.listen(PORT)`, which listens on every interface, and holds a live MCP
  session to `:9001`.

The 2026-10-13 repo audit (finding B1) verified live that **both bypass the login**:

- From the LAN, `GET :8808/tools` → 200. `POST /tools/<name>` runs any tool.
- `:9001` accepted LAN and Tailscale connections. The only barrier was FastMCP's
  DNS-rebinding `Host` check, which a spoofed `Host:` header gets past (→ 406,
  i.e. past the check).
- Every container on the `ai` network reached `host.docker.internal:9001`
  directly. That includes the Hermes/OpenClaw LLM agents, Open WebUI, SearXNG,
  Filebrowser and evals-web.

So any LAN/tailnet device, or a compromised or prompt-injected agent container,
could drive the private canvas with the owner's credentials. That means queueing
renders, listing/viewing assets and metadata, and publishing. ufw is disabled
(audit B2), and a host firewall would be all-or-nothing per port anyway.

Legitimate clients, inventoried before the change:

- **`:9001` (in):** mcpo (Open WebUI's tool path, `MCPO_API_KEY`-gated), the aichat
  bridge on `127.0.0.1`, and the unit's own `ExecStartPost` readiness probe.
- **`:9001` (out):**
  - ComfyUI `127.0.0.1:8189`;
  - the host's own LAN IP `:8189`, for the asset HEAD/GET built from
    `COMFY_MCP_PUBLIC_URL`;
  - DNS on `127.0.0.53`;
  - arbitrary http(s) URLs for `input_image`.
- **`:8808`:** only the local llm-functions shims (`curl http://localhost:8808`).
  Its only upstream is `127.0.0.1:9001`.

Constraints: agents cannot sudo; no fork of upstream comfyui-mcp-server; both
client paths (Open WebUI → mcpo, aichat) must keep working; no GPU disruption.

## Decision
**Reachability is the access control**, so restrict it per service in-kernel and
give mcpo a dedicated path:

1. **aichat bridge — loopback, twice over.**
   - Our llm-functions bridge patch (`config/aichat/llm-functions-bridge.patch`,
     item 4) listens on `MCP_BRIDGE_HOST`, default `127.0.0.1`. This also covers
     interactive `argc mcp start` runs outside systemd.
   - The unit adds `IPAddressAllow=localhost` + `IPAddressDeny=any`.
2. **Secure bridge — systemd IP filter** (cgroup BPF on every socket of the unit,
   including `ExecStartPost`). Inbound packets are judged by source address and
   outbound by destination. An allow match wins; otherwise a deny match drops;
   otherwise the packet passes.
   ```
   IPAddressAllow=localhost 172.31.250.0/29 192.168.4.57/32
   IPAddressDeny=10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 ::/0
   ```
   - `192.168.4.57/32` is the host's own LAN IP, needed for the
     `COMFY_MCP_PUBLIC_URL` asset fetches. Only the host itself can source it:
     the kernel drops inbound packets that claim a local source address as
     martians.
   - Public IPv4 stays allowed for `input_image` URL fetches.
   - FastMCP's `Host` allowlist becomes
     `172.31.250.1:9001,127.0.0.1:9001,localhost:9001`. `host.docker.internal` is
     dropped because that path is now blocked.
   - The bind stays `0.0.0.0`. Binding the Docker gateway IP would race Docker
     creating that interface at boot, and the filter does the scoping.
3. **Dedicated Docker network `mcp-secure`** (compose).
   - Config: `internal: true`, `172.31.250.0/29`, gateway `172.31.250.1` (the
     host side, where mcpo now reaches the bridge).
   - **Only mcpo attaches.** Its `comfyui-secure` upstream is
     `http://172.31.250.1:9001/mcp`.
   - `internal` means no default route through it, so mcpo's other traffic
     (internet, LiteLLM, the open bridge, the llama-swap-mode MCP) stays on `ai`.
   - No other container has an address in that /29.
4. **`scripts/verify-mcp-lockdown.sh`** (read-only, non-root, run by both
   installers). It proves:
   - the installed units match the repo, and the filters are loaded;
   - `:8808` is bound to loopback only;
   - packets from LAN, Tailscale and other-Docker *source addresses* are
     dropped. It simulates these with `curl --interface <host IP>`, which the
     filter judges exactly like a remote client;
   - mcpo reaches the bridge, while every other container is blocked both via
     `172.31.250.1` and via its own gateway;
   - `mcp-secure` membership is exactly mcpo, and mcpo's other upstreams still
     work;
   - read-only `get_queue_status` succeeds end-to-end through both paths.

## Consequences
- **Positive:**
  - LAN and tailnet hosts, and every container except mcpo, lose direct access
    to the credentialed bridges.
  - The remaining remote paths to the secure canvas are ComfyUI's own login on
    `:8189`, or Open WebUI → mcpo (API key) → bridge.
  - Enforcement is per-service in the kernel, independent of ufw (B2) and of the
    application code. It still holds if someone later binds the bridge wider.
  - Side benefit: `input_image` URLs can no longer be used to make the secure
    bridge probe the LAN (SSRF).
- **Negative / trade-offs:**
  - **Addresses to keep in sync:**
    - the host LAN IP, between the unit's `/32` and `COMFY_MCP_PUBLIC_URL` (a
      DHCP change already broke the public URL; now it also breaks asset
      fetches);
    - `172.31.250.0/29` across compose, the unit, and the mcpo `config.json`.

    The verify script checks both.
  - **Anything in the host network namespace still counts as localhost:**
    native services and `network_mode: host` containers (LiteLLM). That is the
    same trust domain as the bridge itself, and accepted.
  - `input_image` URLs on LAN hosts other than this box no longer fetch. Public
    URLs and `data:` URIs still work.
  - Inbound from public IPv4 is not filtered (it can't be denied without also
    blocking outbound fetches). This relies on home-router NAT with no
    port-forward and no global IPv6, which is already a premise of the exposure
    table in `docs/server-setup.md`.
  - mcpo is the trust anchor: anyone holding `MCPO_API_KEY` (Open WebUI's tool
    server; one user today) can drive the secure canvas.
- **Follow-ups / things to watch:**
  - **Adding a client of `:9001` is now a deliberate act.** That means another
    container, a remote agent, or a new mcpo-like proxy. It requires changing the
    unit filter and/or attaching to `mcp-secure`. Never attach anything but mcpo
    there.
  - The open bridge (`:9000` → `:8188`) is intentionally unchanged: the open
    canvas is auth-free by design (ADR-0013).
  - Still open from the audit:
    - ufw (B2);
    - status-page auth (B3);
    - a password reset doesn't restart `comfyui-mcp-secure`, which reads the
      token once at start (B6);
    - `comfyui/login/PASSWORD` is world-readable (0644; low risk on a
      single-user box).
  - If a stdio MCP server that needs network is ever added to the aichat
    bridge's `mcp.json`, extend that unit's `IPAddressAllow` deliberately. Child
    processes inherit the filter.

## Alternatives considered
- **Allow the whole `ai` network** (172.18.0.0/16): this would keep
  `host.docker.internal`. Rejected by the owner because Hermes and OpenClaw are
  tool-using LLM agents and would get unauthenticated access to the private
  canvas.
- **Bind `:9001` to `127.0.0.1` + run mcpo with `network_mode: host`:**
  rejected. It changes mcpo's whole network model and port exposure to fix one
  upstream.
- **Bind to the Docker gateway IP (`172.17.0.1`/`host.docker.internal`):** not
  isolating. Every container reaches the host's addresses via its own gateway.
  It also adds a boot-ordering race.
- **ufw/iptables rules:** host-wide, sudo-managed, and ufw is currently off
  (B2). Docker's own chains complicate container-sourced rules. A systemd filter
  travels with the unit file and is self-documenting.
- **Bearer auth on the bridges:** needs a patch to upstream FastMCP app wiring
  or a reverse proxy in front of both bridges, and client-side header support
  in mcpo and the llm-functions bridge. Possible later as defense-in-depth. The
  network lockdown removes the exposure now with no client changes beyond one
  URL.

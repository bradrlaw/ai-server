# ADR-0028: Host firewall (ufw) with a container→host allowlist

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
Before this change, ufw was installed but **disabled** (audit finding B2,
2026-10-13). Almost every service binds `0.0.0.0`. The only protection was the home
router's NAT. That left three gaps:

1. **Containers could reach every host port.** That includes the prompt-injectable
   agent containers (Hermes, OpenClaw) and Open WebUI. Reachable ports included:
   - SSH `:22` and Samba `:445`;
   - the plan-build MCP `:9100` and AI Toolkit `:8675`;
   - the password-gated ComfyUIs `:8189`/`:8190`.

   ADR-0026 fenced only the two MCP bridges that hold credentials.
2. **No second layer behind NAT.** A router port-forward or UPnP mapping, or a
   future global IPv6 prefix, would expose everything directly.
3. **The old manual recipe in `docs/server-setup.md` was unsafe.** It was
   `ufw default deny` plus allows for the LAN and tailnet, then `ufw enable`.
   - It would have **broken the stack**: containers aren't on the LAN subnet, so
     Open WebUI → LiteLLM `:4000`, ComfyUI `:8188` and the MCP bridges would all
     have been denied.
   - Its `allow 22/tcp` still let containers reach SSH.
   - It didn't cover **Docker-published ports**. IPv4 publishes are DNATed into
     `FORWARD`, so they never reach ufw's `INPUT` rules. This is the well-known
     ufw + Docker hole.

**What containers legitimately need from the host.** Inventoried from `docker/`, the
live Hermes/OpenClaw configs and Open WebUI's database:

| Port | Caller → service |
| --- | --- |
| 4000 | Open WebUI, Hermes, OpenClaw → LiteLLM |
| 8188 | Open WebUI image generation and functions → ComfyUI (open) |
| 9000 | mcpo → ComfyUI MCP bridge (open) |
| 9001 | mcpo → secure ComfyUI MCP bridge, at `172.31.250.1` (ADR-0026) |
| 9095 | Open WebUI "Server Status" filter → status JSON |
| 9120 | mcpo → llama-swap-mode MCP |

Two configured targets already fail today, and the firewall keeps them failing
fast:
- Open WebUI's Ollama URL `host.docker.internal:11434` (nothing listens there);
- the status filter's default `:9090` (llama-swap binds loopback only).

**One configuration would have broken.** Open WebUI's admin tool server
`llama-swap-mode` pointed at `http://aipcub.local:8000/…`. That goes from the
container to the host's LAN IP and through docker-proxy, so the new reject rule
would block it. The owner is switching it to `http://mcpo:8000/llama-swap-mode`,
container DNS like the other six. `verify-firewall.sh` now checks for this.

**Lockout risk.** The box is headless and managed over SSH only, so a firewall
mistake can lock the owner out.

**Owner's decision:** allowlist only the ports containers use, and reject the rest.

## Decision
1. **ufw on.**
   - Default: deny incoming, allow outgoing.
   - Routed traffic stays deny (`DEFAULT_FORWARD_POLICY=DROP`, unchanged). Docker's
     own `FORWARD` chains keep handling container traffic.
2. **Rules, in evaluation order.** Each managed rule carries an `ai-fw:<hash>` ufw
   comment.
   1. **Containers → host.** `172.16.0.0/12` covers every Docker bridge network.
      - Allow TCP `4000,8188,9000,9001,9095,9120` from those sources.
      - Then **REJECT** everything else from them.
      - Both rules are inserted at the **top**. Otherwise the broader allows below,
        such as SSH from anywhere and Samba's 445 rules, would also match container
        sources.
      - REJECT rather than DROP, so a misconfigured caller fails at once instead of
        hanging until its timeout. ufw renders this rule as a plain `-j REJECT`
        (ICMP port-unreachable), which a TCP client sees as "connection refused".
   2. **SSH `22/tcp` from anywhere.** Anti-lockout. The existing rule is kept and
      tagged.
   3. **The tailnet.** Everything on `tailscale0`, plus `41641/udp` for Tailscale
      direct connections.
   4. **The LAN:**
      - `192.168.4.0/22`;
      - the on-link IPv6 ULA prefix, auto-detected from RA routes (only `fc00::/7`
        is trusted automatically; global prefixes are not);
      - IPv6 link-local on `eno1`.

   The existing Samba 445 rules (ADR-0022) stay. They're redundant now but harmless.
3. **Docker-published ports.** A managed block in `/etc/ufw/after.rules` owns the
   `DOCKER-USER` chain. Each of its two parts logs (rate-limited) before it blocks.
   1. **Non-LAN sources.** It drops **NEW** connections that arrive on `eno1` from
      outside the LAN subnet.
      - Not affected: tailnet traffic (it arrives on `tailscale0`), LAN traffic, and
        container↔container traffic by container address.
      - Logged as `[AI-FW DOCKER BLOCK]`.
   2. **Containers going through the host.** A container that dials a published port
      via the host's address *from another Docker network* is DNATed and
      **forwarded**. That path never passes ufw's `INPUT` rules, so rule 2.1 never
      sees it. (Measured before the change: `autoheal` → `<LAN IP>:3000` connected.)
      - The block matches `-s 172.16.0.0/12` with `--ctstate DNAT --ctdir ORIGINAL`
        and REJECTs: `tcp-reset` for TCP, ICMP port-unreachable for everything else.
      - `ORIGINAL` only, so replies to LAN or tailnet clients of a published port
        still pass; their source is the container.
      - The rule catches every DNATed container connection, whichever network it
        comes from. Same-network hairpins that docker-proxy handles instead arrive
        on `INPUT`, where rule 2.1 rejects them.
      - Logged as `[AI-FW DOCKER XNET]`.

   IPv6 publishes go through docker-proxy, i.e. through `INPUT`, so rules 2–4 cover
   them.
4. **Safe rollout: `scripts/setup-firewall.sh`.** The owner runs it with sudo.
   - **Plan without root.** `--dry-run` prints the plan.
   - **Pre-flight.** The script refuses to start unless all of these pass:
     - sshd is listening, and the script has a terminal to ask on;
     - `/etc/default/ufw` has `IPV6=yes`;
     - no earlier run is still unconfirmed (no `PENDING` marker, no armed or running
       rollback), and no `rollback.sh` is running: the run takes the rollback lock
       here (see "One rollback at a time");
     - `ufw.service` is enabled (the boot safety net hooks into it) and the boot ID
       is readable;
     - for IPv4 and IPv6 (both mandatory), ufw's rules are loaded exactly when
       `ufw.conf` says it is enabled (when it is disabled, none of ufw's chains may
       still hold a rule), and every `INPUT`/`OUTPUT`/`FORWARD` policy reads as
       `ACCEPT` or `DROP` (the rollback records and restores them). Each family's
       whole filter table is read at once (`iptables -S`), so a read that fails
       stops the run instead of passing for "chain absent";
     - `DOCKER-USER` holds no rules from anyone else. It must be visible while Docker
       runs; with Docker stopped, the block applies when ufw loads;
     - every rule passes `ufw --dry-run`;
     - the block passes `iptables-restore --test`;
     - `verify-firewall.sh --preflight` passes: the allowlisted ports connect,
       every Open WebUI admin tool server is reachable without going through the
       host, and DNS/egress work.
   - **Snapshot and dead-man switch.**
     - Everything goes to `/etc/ufw/ai-fw-snapshot/`:
       - copies of `user.rules`, `user6.rules`, `after.rules`, `ufw.conf` and
         `/etc/default/ufw` (as `default-ufw`), each compared with `cmp`;
       - `docker-user.rules`: the live `DOCKER-USER` rules;
       - the generated `rollback.sh`, which also records whether ufw was enabled and
         every chain policy;
       - `PENDING` (run stamp + boot ID), written after the snapshot. Both are
         `sync`ed to disk before the timer is armed or anything changes.
     - **Taken after the `y`, checked against the plan.** The prompt can wait
       indefinitely, so the snapshot is built in a staging directory and checked
       against what the plan was built from: the config files must match their
       pre-prompt checksums, and the live state (ufw loaded, policies,
       `DOCKER-USER`) must read the same again. Otherwise the run stops with nothing
       changed. Only then does the staged snapshot replace the previous one (a kept
       run's undo target), so it is never a mix of two runs. A run that dies between
       the swap's two renames puts the previous snapshot back as it exits; if it was
       killed outright, the next run does.
     - A transient systemd timer (`CONFIRM_SECS`, default 300 s) runs
       `rollback.sh --deadman` unless the owner tests a **new** SSH session plus the
       web UIs and then types `keep`. If that rollback fails, systemd retries it
       every 30 s, up to 10 times within an hour.
     - Ctrl-C, a dropped SSH session, no answer or any error rolls back at once.
     - **Reboot or power loss inside the window:** a `ufw.service` drop-in
       (`scripts/ai-fw-ufw-rollback.conf` →
       `/etc/systemd/system/ufw.service.d/ai-fw-rollback.conf`, installed by the
       script, which checks that systemd loaded it) runs `rollback.sh --boot` before
       ufw loads its rules.
       - It does nothing unless the run's `PENDING` marker exists, and nothing
         within the same boot (the marker's boot ID matches; the timer owns that
         case).
       - It restores the files, so ufw loads the pre-run config.
       - If it fails, `ufw.service` fails and loads **no** rules. The host boots
         open and reachable over SSH (fail-open) rather than with an unconfirmed
         firewall.
     - **One rollback at a time.** The rollback lock, `/run/ai-fw-rollback.lock`,
       lives outside the snapshot directory, which a re-run replaces, and is
       root-only (0600: anyone who could open it could hold it and stall a
       rollback). The script takes it at pre-flight and holds it until it asks for
       `keep`, so no rollback runs while it snapshots, applies and verifies: a timer
       that fires meanwhile waits instead of restoring underneath a half-applied
       change. `keep` clears `PENDING` under the same lock, so a dead-man run that
       was already waiting finds nothing to do. If the timer already fired, `keep`
       reports "too late" and the run rolls back. Each `rollback.sh` restores only
       its own run's snapshot: one that waited out a re-run (say, a manual undo
       started meanwhile) finds a later run's snapshot in place and exits without
       changing anything.
     - **Every rollback verifies the live state, not exit codes:**
       - the restored files match the snapshot and are `sync`ed to disk;
       - ufw's IPv4 and IPv6 rules are loaded, or fully unloaded, as before. ufw's
         stop ignores flush errors and leaves its main chains behind (empty, still
         jumped to: harmless), so "unloaded" means its user chain is gone and none
         of its chains holds a rule. Before re-enabling, the disable must have
         removed the user chain, or ufw's start would do nothing;
       - every chain policy is as recorded;
       - `DOCKER-USER` holds exactly the snapshot's rules. Restoring a declared
         chain flushes it on both iptables backends (checked in the 1.8.10 source)
         and creates it if it's gone; an explicit flush covers an empty snapshot.
         A missing chain passes only if the snapshot was empty too. If Docker was
         stopped, the chain ufw created stays, empty; Docker adopts it when it
         starts.

       Each check reads the family's whole filter table; a table that can't be
       read is a failure, never "absent". A failing `ufw disable` is only a
       warning, because everything it affects is checked afterwards. At boot the
       files are the only thing to restore, since ufw then loads them.

       On any failure it prints `ROLLBACK FAILED` and keeps `PENDING` and the
       snapshot, so the timer's retries and the next boot try again. It also prints
       the command for retrying by hand.
   - **Strict verify before asking.** After applying, it runs `verify-firewall.sh`
     with root. Any FAIL, or an inconclusive result (exit 3), rolls back
     automatically; the owner is never asked to keep a firewall that didn't pass.
   - **Fail-closed rule changes.** When the container pair changes (new ports),
     two transient rejects (`tcp`/`udp` from `172.16.0.0/12`) go on top first. Then
     the old pair is deleted, the new pair is inserted above them, and the
     transients are removed. Containers can't fall through to "SSH from anywhere"
     in between; allowlisted ports are refused for those few seconds.
   - **Idempotent.** Re-run it to add a port, or after a LAN or prefix change:
     - stale managed rules are pruned;
     - the container rules are moved back to the top when they change;
     - hand-added rules are never touched;
     - a re-run with nothing changed leaves the rules and `after.rules` as they are.
   - **Undo a kept run:** `sudo /bin/sh /etc/ufw/ai-fw-snapshot/rollback.sh`
     restores the state from before the last run.
5. **Verification: `scripts/verify-firewall.sh`.** It probes from inside the
   containers:
   - the allowlist still connects;
   - every other **listening** host port is refused fast: SSH, plan-build, AI
     Toolkit, ComfyUI secure and qwen, Samba on the LAN IP, and Open WebUI's
     published `:3000` via the LAN IP (a same-network hairpin);
   - **cross-network:** a container on another Docker network (`autoheal`) can't
     reach a published port via the host's LAN IP;
   - Open WebUI's admin tool servers are reachable. This check **fails closed**: an
     unreadable config or an incomplete check is a FAIL, not a skip;
   - DNS and egress work.

   With sudo it also checks the ufw state, the rule order, the managed
   `DOCKER-USER` rules and that no unconfirmed run is pending. Exit codes: `0`
   pass, `1` FAIL, `2` usage, `3` inconclusive (full mode without root, so the
   rule-level checks didn't run).

## Consequences
- **Positive:**
  - A compromised or prompt-injected container reaches only the six services it
    needs. SSH, Samba, plan-build, AI Toolkit and the password-gated ComfyUIs are
    refused, and so are published container ports dialled via the host's address,
    from any Docker network.
  - Non-LAN sources can't reach host services or published container ports (except
    SSH) even if a port-forward or UPnP mapping appears.
  - IPv6 inbound from outside the LAN is denied by default.
  - The rollout can't strand the owner. On any doubt, including a failed
    post-apply verify, a dropped session or a reboot mid-window, it rolls back
    automatically.
- **Negative / trade-offs:**
  - **New container→host dependencies need a re-run.** Add the port to
    `DOCKER_HOST_PORTS` (the default in both scripts) and re-run the installer.
    Until then the new dependency gets "connection refused".
  - **Containers must reach sibling containers by Docker DNS name** (e.g.
    `http://mcpo:8000`), never by the host's IP, hostname or Tailscale IP plus a
    published port. That path either hairpins through docker-proxy (same network)
    or is DNATed and forwarded (another network); both are rejected.
  - **The allowlist is per port, not per container.** For example, Hermes can reach
    ComfyUI `:8188`. Per-network scoping would depend on Docker-assigned bridge
    subnets. `:9001` keeps its own systemd filter (ADR-0026).
  - SSH stays reachable from any source. That is deliberate (anti-lockout, behind
    NAT).
  - **The managed block owns `DOCKER-USER`** and flushes it on every ufw load. Any
    other `DOCKER-USER` rule must go inside the block.
  - The LAN rules are derived from the current subnet and ULA prefix. If the
    router or ISP changes them, re-run the installer (SSH stays open regardless).
    LAN hosts using a **global** IPv6 address (none today) aren't trusted
    automatically; set `LAN_V6`.
  - Blocked packets are logged (ufw `LOGLEVEL=low`; `[AI-FW DOCKER BLOCK]` for the
    non-LAN published-port drop, `[AI-FW DOCKER XNET]` for the container reject).
  - The `ufw.service` drop-in stays installed. It does nothing unless a run is
    unconfirmed. If its rollback ever fails at boot, ufw stays unloaded (the host is
    open, with Docker's own rules only) until the owner fixes it; `systemctl status
    ufw` shows the failure. It can be removed after the rollout (see
    `docs/server-setup.md`).
  - **No other root process may change the firewall during a run.** The live state
    is re-checked after the prompt, not continuously. The rollback restores the
    rules in `DOCKER-USER`, not Docker's jump to it from `FORWARD`, which neither
    the script nor ufw touches.
- **Follow-ups / things to watch:**
  - mcpo's `/docs` and `openapi.json` are still readable without the API key from
    LAN, tailnet and containers (audit sub-point; not addressed here).
  - Optional per-container scoping of the allowlist.

## Alternatives considered
- **Leave ufw off and rely on NAT:** rejected. It is audit finding B2, and it gives
  no container→host isolation.
- **The old manual recipe:** rejected. It would break Open WebUI → LiteLLM/ComfyUI
  and the MCP bridges, while still letting containers reach SSH, and it misses
  Docker-published ports.
- **Allow all container→host traffic** (`allow from 172.16.0.0/12`): simplest, with
  no breakage risk. The owner rejected it because agent containers could still
  reach SSH, Samba and the other host services.
- **DROP instead of REJECT for containers:** misconfigured callers (the Open WebUI
  Ollama probe, the status filter's `:9090`) would hang for the full TCP timeout.
- **The `ufw-docker` helper (third-party):** it rewrites more of `after.rules`. Our
  block uses the same core pattern, kept minimal and managed in the repo.
- **Docker `"iptables": false`:** breaks container networking and port publishing.
- **Binding published ports to specific IPs:** would drop tailnet access unless
  duplicated per address, and is brittle under DHCP.

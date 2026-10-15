# ADR-0022: Share /srv/ai over SMB (Samba) on Tailscale + LAN

- **Status:** Accepted
- **Date:** 2026-09-13
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
Managing files under `/srv/ai` (models, ComfyUI outputs, LoRAs, datasets) from the
built-in web file browser is slow for bulk moves, renames, and cleanup. The owner wants
to mount `/srv/ai` from macOS Finder and Windows Explorer using their native file
managers, which speak **SMB3** first-class.

Relevant state at time of writing:
- The box (`aipcub`, `100.97.143.29`) already runs **Tailscale**; the owner's Mac
  (`bradi9mac-2`) and Windows (`bradeb`) machines are on the same tailnet.
- LAN: `eno1` = `192.168.4.57/22`. Docker bridges (`docker0`, `br-*`) also present.
- `ufw` is installed. No Samba installed yet.

Alternatives considered:
- **SSHFS over the existing SSH** — rides current auth, but macOS dropped clean
  FUSE/SSHFS support (kext/Gatekeeper friction); poor Finder UX.
- **Taildrive** (`tailscale drive`, built-in WebDAV) — no daemon, tailnet-only, but
  WebDAV in Finder is slow/clunky for bulk file operations.
- **NFS** — weak host-based auth, awkward across mixed macOS/Windows.

## Decision
**Install Samba and export `/srv/ai` as a single SMB3 share (`srv-ai`), bound to
loopback + `tailscale0` + `eno1` only.**

- Canonical config is repo-tracked at [`config/smb.conf`](../../config/smb.conf);
  deployed by [`scripts/setup-samba.sh`](../../scripts/setup-samba.sh) (run with sudo by
  the owner — agents cannot sudo). The script installs Samba, copies the config (backing
  up any existing `/etc/samba/smb.conf`), validates with `testparm`, sets the SMB
  password for `brad`, adds `ufw` rules, and enables `smbd`.
- **Security posture:**
  - `bind interfaces only = yes`, `interfaces = lo tailscale0 eno1` — smbd never listens
    on the docker/`br-*` bridges or the public internet. **Port 445 is never exposed to
    the internet.** *(Superseded 2026-10-15: this binding never covered the tailnet.
    See the addendum below.)*
  - `server min protocol = SMB3_11`, `smb encrypt = required` — SMB1/2 refused, transport
    encrypted (belt-and-suspenders over the already-encrypted WireGuard tailnet, and the
    only crypto layer on the LAN path). Modern Finder / Windows 10/11 negotiate this
    fine; relax to `desired` only if a client can't mount.
  - `map to guest = never`, `restrict anonymous = 2`, `valid users = brad` — authenticated
    access only, via a dedicated SMB password (separate from the login password).
  - `ufw` rules scope 445 to `tailscale0` + `192.168.4.0/22`; the script also
    `ufw allow 22/tcp` and does **not** auto-enable ufw (avoids an SSH lockout).
    *(Update 2026-10-14: ufw is now enabled by `scripts/setup-firewall.sh`,
    [ADR-0028](0028-host-firewall-ufw.md). Its LAN and tailnet allows cover 445, so
    these rules are redundant but harmless.)*
- **Access surface:** Tailscale **+ LAN** (owner's choice — faster local throughput),
  accepting that SMB is reachable to trusted LAN hosts in addition to the tailnet.
- **Scope:** the full `/srv/ai` tree (owner's choice — full cleanup/model management),
  with `nmbd`/NetBIOS disabled (SMB3 needs no browse master).
- macOS interop via `vfs_fruit` (`catia fruit streams_xattr`) for correct resource-fork /
  `.DS_Store` handling.

## Consequences
- Positive: Native Finder/Explorer multi-file and cleanup operations on `/srv/ai`;
  encrypted, authenticated, non-internet-facing; reproducible + documented in-repo.
- Negative / caveats:
  - **Root-owned files** under `/srv/ai` (e.g. some model/LoRA files created via sudo)
    cannot be modified/deleted over SMB as `brad`; fix per-directory with `chown` as
    needed (the setup script reports the count).
  - SMB is now reachable from the LAN, not only the tailnet — acceptable for a trusted
    home network; tighten to tailnet-only by dropping `eno1` from `interfaces` and the
    LAN `ufw` rule if that changes.
  - `smb encrypt = required` could block a legacy client; documented relaxation to
    `desired` if needed.

## Addendum 2026-10-15: listen on all addresses, filter sources (audit B4)

**Finding.** SMB over Tailscale never worked: smbd listened on loopback and the LAN
only, never on `100.97.143.29`. Two Samba behaviours (4.19.5) defeated
`bind interfaces only = yes`:
- **`tailscale0` is skipped.** It is a point-to-point interface without
  `IFF_BROADCAST`, and Samba's interface probe (`source3/lib/interface.c`,
  `add_interface`) drops such interfaces ("not adding non-broadcast interface") even
  when they are listed by name. Listing the address as `100.97.143.29/32` doesn't
  help: it resolves back to the same probed interface and is dropped again.
- **Addresses are bound once, at startup.** smbd opens one socket per address when
  it starts and never re-binds. `eno1` uses IPv6 privacy addresses, which rotate
  daily, so after a few weeks 7 of its 8 LAN IPv6 listeners were on privacy
  addresses that had already rotated out.

**Decision.** `bind interfaces only = no` (smbd listens on `0.0.0.0:445` and
`[::]:445`), and access is restricted by source address with
`hosts allow = 127.0.0.1 ::1 192.168.4.0/22 100.64.0.0/10 fc00::/7 fe80::/10` plus
`hosts deny = ALL`. Samba checks this on every connection before parsing a single
SMB packet; denied clients get a NetBIOS negative session response and the
connection closes. `fc00::/7` covers both the router-assigned LAN ULA prefix and
the tailnet's `fd7a:115c:a1e0::/48`; ufw narrows it to the detected LAN prefix.
Docker networks (`172.16.0.0/12`) are not listed, and ufw (ADR-0028) rejects
container → host 445 as well, so there are two independent layers. `smb ports = 445`
drops the legacy NetBIOS session port 139 (nmbd was already disabled).
`interfaces = lo eno1` now only scopes the addresses advertised for SMB3 multichannel
(the mDNS registration covers all interfaces either way). `setup-samba.sh` no longer
adds its own IPv4-only ufw rules (ADR-0028 owns the firewall). It now checks that
smbd really listens on the wildcard addresses after the restart.

Rejected alternatives: keep the binding and add `100.97.143.29/10` (a wide mask makes
Samba synthesize a broadcast interface). That still leaves stale IPv6 listeners, needs
smbd ordered after `tailscaled`, and breaks if the tailnet IP changes. Dropping
Tailscale from the docs was also rejected, because remote access is the point.

**Verification (2026-10-15, before deploying).** The repo `smb.conf` ran in an
unprivileged throwaway smbd on a high port. From each local source address an
SMB 3.1.1 NEGOTIATE succeeded: loopback v4/v6, LAN v4, LAN ULA (stable and
temporary), LAN link-local, tailnet v4/v6/link-local. From `172.31.250.1` (a
non-NAT docker network) to the loopback, LAN and tailnet addresses it was denied
with "Connection denied". Probing other docker subnets this way is not possible:
docker's MASQUERADE rewrites a locally bound `172.17-19.x` source to the LAN
address. Docker-bridge link-local (`fe80::/10`) passes Samba's list, but ufw only
admits link-local on `eno1`.

**Consequence.** If the LAN subnet changes, update `hosts allow` (ufw re-detects its
own). Never return to per-interface binding without re-checking both pitfalls above.

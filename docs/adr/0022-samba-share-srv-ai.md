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
    the internet.**
  - `server min protocol = SMB3_11`, `smb encrypt = required` — SMB1/2 refused, transport
    encrypted (belt-and-suspenders over the already-encrypted WireGuard tailnet, and the
    only crypto layer on the LAN path). Modern Finder / Windows 10/11 negotiate this
    fine; relax to `desired` only if a client can't mount.
  - `map to guest = never`, `restrict anonymous = 2`, `valid users = brad` — authenticated
    access only, via a dedicated SMB password (separate from the login password).
  - `ufw` rules scope 445 to `tailscale0` + `192.168.4.0/22`; the script also
    `ufw allow 22/tcp` and does **not** auto-enable ufw (avoids an SSH lockout).
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

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

**Deployed (2026-10-15 06:18 UTC).** The owner ran `sudo scripts/setup-samba.sh`.
smbd listens on `0.0.0.0:445` and `[::]:445` only (no 139; nmbd disabled), and
`/etc/samba/smb.conf` matches the repo. The same probes against the live :445
gave the same results: every LAN, tailnet and loopback source negotiated SMB
3.1.1, and `172.31.250.1` was refused before SMB parsing. The owner confirmed SMB
works over Tailscale. The installer leaves ufw alone, so two older `445` rules
(tailscale0, and `192.168.4.0/22`) remain. They are redundant: the ADR-0028
firewall already admits all tailnet and LAN traffic.

**Consequence.** If the LAN subnet changes, update `hosts allow` (ufw re-detects its
own). Never return to per-interface binding without re-checking both pitfalls above.

## Addendum 2026-10-16: hide credential stores from the share (audit E8)

**Finding.** The share served all of `/srv/ai` to anyone with brad's SMB password,
including every credential store on the box and, while unlocked, the encrypted
volume. The 0700/0600 modes from the ADR-0013 addendum don't help here: smbd serves
the share as brad, who owns these files. A scan of every visible file for the secret
values held in those stores (117 values from 22 store files; 640,259 files, 32.1 GB; plus
the contents of the backup archives and the git history of the 7 repos under
`/srv/ai`) found three more copies:
- `comfyui/user/comfyui_8189.log` (and `.prev`, `.prev2`): the secure instance's API
  token. ComfyUI-Login logs it on every start, and ComfyUI-Manager copies startup
  output to `user/comfyui_<port>.log` (ADR-0013).
- `storage-bulk/backups/owui/owui-data-20260806-032115.tgz`, a one-off Open WebUI
  volume backup (root-owned, 0644). Its `webui.db` held the LiteLLM master key, the
  mcpo API key and the Open WebUI API key, plus 24 chats and 33 uploads.
- `storage-bulk/backups/comfyui/20260803-065113/comfyui-user.tgz`: older copies of the
  same logs, with the same token.

All other hits were false positives: PEM armour lines (`-----BEGIN ...-----`; no key
body matched) and a stored field whose value is just the env-var name
`OPENCLAW_GATEWAY_TOKEN`.

**Decision (owner, 2026-10-16).** Hide every credential store, not only the two the
audit named, with Samba `veto files` on `[srv-ai]`:

| Name | Hides |
|---|---|
| `.env` | `docker/.env` (gateway keys and tokens) |
| `server-status.env` | the status service's Open WebUI API key |
| `auth.json` | the status dashboard password hash |
| `PASSWORD`, `.secret-key.txt` | ComfyUI-Login, all instances |
| `aitk_db.db*` | ai-toolkit's database and its `-wal`/`-shm` files (hold the HF token) |
| `auth-secrets`, `state` | OpenClaw credentials and device identity |
| `openclaw.bak-*` | copies of the above made by upgrades |
| `hermes` | Hermes home (its config holds the LiteLLM key) |
| `backups` | every backup folder |
| `comfyui_*.log` | ComfyUI-Manager logs |
| `storage` | the encrypted volume while unlocked |
| `input-secure`, `output-secure`, `temp-secure` | ComfyUI's links into `storage/` |

- **Backups:** the owner deleted both August backups ("the rule will protect future
  backups"). Future backups go in a folder named `backups`; `storage-bulk/backups/`
  is kept for that and is now mode 0700.
- **Manager logs:** hidden by name. Manager's file logging stays on.
- `delete veto files = no` (the default, pinned): a folder that holds hidden items
  can't be deleted over SMB, instead of silently taking the hidden items with it.
- Not used: `hide files`. It only sets the DOS hidden attribute, so the item is
  listed whenever a client shows hidden files, and it stays openable.

**How a veto behaves (Samba 4.19.5, read in the source and tested).** A vetoed name
is invisible and unopenable: listings skip it, and open, create, overwrite, mkdir,
rename-to and delete all answer "not found". Samba checks every path component
(case-insensitive, `*` and `?` wildcards, any depth), so nothing under a vetoed
folder is reachable, whether through a symlink into it, a `./` or `../` spelling, an
alternate data stream, an 8.3 short name or a search pattern. Links that leave the
share are refused (`wide links = no`), and SMB clients can't create real symlinks.

**Limits.**
- It matches names, not targets. A copy or hardlink under another name is visible,
  and so is a symlink with an innocent name that points straight at a hidden item
  (Samba follows the last path component without checking its target). None exist
  today; `scripts/verify-smb-veto.py` checks for them.
- Every same-named item is hidden too: `docker/hermes/` (the Hermes template),
  `comfyui/backups/` (`comfyui-snapshot.sh` package lists), `tools/llm-functions/.env`
  (no secrets), the open instance's `comfyui_8188*.log` and the old
  `comfyui_8199*.log`, plus 15 items inside dependency and cache trees. Use SSH for
  these.
- SSH still sees everything. filebrowser (`:8083`, own login) mounts the secure input
  and output folders on purpose (ADR-0023) and none of the stores.
- ComfyUI's asset databases (`comfyui/user/comfyui-*.db`) stay visible. They hold no
  assets today. If ComfyUI starts indexing secure renders there, the file names will
  show.

**Found on the way: mcpo held a copy of the unlocked volume.** mcpo bind-mounts
`/srv/ai` read-only at `/repos/ai-server` for its `git` MCP server. Docker binds are
recursive by default, and the volume was unlocked when mcpo last started
(2026-10-15 15:05 UTC), so mcpo got its own copy of the volume's mount (and of
`storage-bulk`). 855 files in it were readable by mcpo's uid 10001; before the B5
change earlier that day, mcpo ran as root. A host `umount` leaves such a copy in
place, so `storage-crypt.sh lock` would unmount on the host and then fail at
`cryptsetup close` (device in use), leaving the volume decrypted. Fixed with the
owner's approval:
- The bind is now non-recursive (`bind: recursive: disabled`). mcpo sees the repo,
  but `storage/` and `storage-bulk/` are empty folders inside it. After the recreate
  (2026-10-16 05:18 UTC), mcpo had no copy of either mount, all 7 MCP routes
  answered, `git_status` and `git_log` worked, and `scripts/verify-mcp-lockdown.sh`
  gave 31 passed, 0 failed, 1 skipped, as before.
- When `cryptsetup close` fails, `storage-crypt.sh lock` now lists the processes and
  containers that still have the volume mounted, and `status` shows them under "also
  mounted in". While unlocked that is normally just filebrowser, which `lock` stops
  first. Tested with `cryptsetup`, `mountpoint` and `require_root` stubbed: busy with
  a holder (listed, exit 1), busy without one (exit 1), success (exit 0).

**Verification (before deploying).**
- An unprivileged throwaway smbd on 127.0.0.1 served the real tree read-only with the
  repo's veto list, next to a copy of the share without it and a synthetic share:
  181 checks passed, 0 failed, 5 informational. They covered listings, opening every
  vetoed path (refused on the share, readable on the copy without the veto), case,
  stream, `./` and `../` spellings, search patterns, 8.3 names, symlinks into, at and
  out of hidden items, the new names against lookalikes (`comfyui.log`,
  `comfyui-8189.log`, `backups.txt` and `old-backups` stay visible), and write, rename
  and delete attempts (refused, disk unchanged).
- `scripts/verify-smb-veto.py` (new; read-only, stdlib only, no sudo) checks that the
  deployed config matches the repo, that `delete veto files` and `wide links` are off,
  that every store on its list is hidden, and that no visible symlink, hardlink or bind
  mount exposes a hidden item. With `--repo-only`: 35 passed, 0 failed.

**Deploy.** `sudo /srv/ai/scripts/setup-samba.sh` (restarts smbd, so mounted clients
reconnect), then `scripts/verify-smb-veto.py`.

**Exposure.** The Open WebUI backup kept the LiteLLM master key, the mcpo API key and
the Open WebUI API key readable over SMB (as brad) and from inside mcpo from
2026-08-06 until it was deleted on 2026-10-16. During this audit the agent also
printed the LiteLLM master key into its session log (`~/.copilot`, mode 0700) and
its model conversation. No other account has had shell access (owner, 2026-10-15).

**Rotation (owner, 2026-10-16).** The LiteLLM master key was rotated at 05:48Z.
The new key is in `docker/.env`, `hermes/config.yaml`, `~/.config/aichat/config.yaml`
and Open WebUI's stored connection key. litellm, open-webui, mcpo and openclaw were
recreated, hermes restarted. `plan-build-mcp` loads `docker/.env` only at start, so it
also needed `sudo systemctl restart plan-build-mcp`. The old key now gets the same 400 as
an unknown key. Open WebUI, Hermes, aichat and OpenClaw were checked end to end. The
mcpo and Open WebUI API keys were not rotated (owner). Procedure: runbook,
"Rotating the master key".

**Deployed (2026-10-16 06:01 UTC).** The owner restarted `plan-build-mcp` and ran
`sudo scripts/setup-samba.sh`. `/etc/samba/smb.conf` matches the repo, and
`scripts/verify-smb-veto.py` passed 38 checks with 0 failures. `plan-build-mcp` runs
with the new key: a `fast_make_plan` call through `:9100` returned a plan. Since the
rotation, LiteLLM has rejected only the agent's own old-key and random-key tests, so
no client was left on the old key. The owner set the new key on the Mac and deleted
`hermes/config.yaml.bak-20260721T202912Z`, which held the old one.

**Keeping it working.**
- New credential store: add its name to `veto files` in `config/smb.conf` and to
  `STORES` in the verify script, re-run the installer, then run the verify script.
- Back up into a folder named `backups` with mode 0700 (audit E7 builds on this).
- Never create a link or hardlink under another name that points at a hidden item.
- A container that bind-mounts `/srv/ai`, or any parent of `storage/`, must use a
  non-recursive bind, as mcpo does.

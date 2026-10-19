# ADR-0013 — Dual ComfyUI instances (open + password-locked), one per V100

* Status: Accepted
* Date: 2026-07-07
* Deciders: @bradrlaw

> **Later changes (noted 2026-10-15):** a third, on-demand ComfyUI instance for Qwen-Image 2.1
> (`comfyui-qwen`, `:8190`, idx1) was added in
> [ADR-0024](0024-qwen-image-2.1-comfyui-instance.md). The P100 mentioned below died on
> 2026-07-27; idx0 is a 12 GB Titan X stopgap
> ([ADR-0017](0017-p100-hbm-ecc-death-titan-x-stopgap.md)).

## Context

ComfyUI has **no real built-in authentication or multi-tenant isolation**. The
native `--multi-user` flag only partitions workflows/settings by a *spoofable*
`comfy-user` header and does **not** isolate the `output/` folder — every user
still sees every generated image. We wanted:

1. A **locked** ComfyUI (login required) for private work.
2. An **open** ComfyUI for casual/shared use and to keep backing the existing
   MCP / Open WebUI image-gen integration (which posts to `127.0.0.1:8188` with
   no auth).
3. **No GPU contention between the two** — the concern that drove this design.
4. Shared models + workflows, but **separate inputs/outputs**.

The box has 2× V100-32GB (idx1, idx2) + 1× P100-16GB (idx0). Both V100s are also
the homes for the LLM tier (`coding`→idx1, `chat`→idx2; `big`/`agentic` span
both). The P100 (16GB) cannot host the 27B/35B chat/coding models, so the V100s
cannot be *statically* handed to ComfyUI without losing the LLM tier.

## Decision

Run **two native systemd ComfyUI services from the same install**, each pinned to
its own V100 via `CUDA_VISIBLE_DEVICES` (+ `CUDA_DEVICE_ORDER=PCI_BUS_ID`):

| | OPEN (`comfyui-open.service`) | LOCKED (`comfyui-secure.service`) |
|---|---|---|
| Port | 8188 | 8189 |
| GPU | idx1 / V100 #1 | idx2 / V100 #2 |
| Auth | none | ComfyUI-Login (shared password) |
| Role | MCP + Open WebUI + open canvas | private canvas |
| free_gpu evict / restore | `coding` / keep chat,fast | `chat` / keep coding,fast |
| output / input / temp | `*-open` | `*-secure` |

* **Native, not containerized** (per ADR-0006 "GPU tiers run native"). Reuses the
  existing venv, custom nodes, and 40GB+ of models with zero rebuild; GPU pinning
  via `CUDA_VISIBLE_DEVICES` is as hard a boundary as a container `--gpus` for a
  single-tenant-per-card instance.
* **Shared**: `models/`, `custom_nodes/`, and `--user-directory` (→ workflows +
  UI settings). Each instance uses its **own** `--database-url`
  (`user/comfyui-open.db` vs `user/comfyui-secure.db`) — ComfyUI's sqlite DB
  cannot be locked by two processes, so a shared DB path throws
  "Could not acquire lock on database". Separate DBs avoid this; workflows
  (files under `user/default/workflows`) are still shared.
* **Separate**: `--output-directory` / `--input-directory` / `--temp-directory`
  per instance — this is the asset-isolation boundary between open and locked.
* **Auth isolation trick**: ComfyUI-Login is installed into an *isolated*
  `custom_nodes_secure/` dir that is added to the search path **only** on the
  locked instance via `--extra-model-paths-config comfyui-secure-extra-paths.yaml`
  (a `custom_nodes:` key, supported by `utils/extra_config.py`). The open instance
  never loads the login node, so `:8188` stays auth-free for the MCP. The login
  password is bcrypt-hashed at `/srv/ai/comfyui/login/PASSWORD`.
* **GPU coexistence with the LLM tier** stays *dynamic*: the shared `free_gpu`
  hook unloads each instance's card-mate LLM on generate and warms it back after
  an idle window, so LLMs use the V100s while ComfyUI is idle. Image gen is
  bursty, so this is acceptable. `big`/`agentic` (which span both V100s) will
  briefly contend with either instance and reload on demand.

## Consequences

* No ComfyUI-vs-ComfyUI GPU contention (dedicated cards). ✅
* Existing MCP / Open WebUI image-gen keeps working unchanged (still `:8188`). ✅
* Heavy image use on both cards can transiently evict *both* daily LLMs
  (`coding`, `chat`); they reload automatically. Acceptable for a family box.
* Shared `--user-directory` means both instances share workflows **and** UI
  settings; concurrent settings writes could race (rare). Each instance has its
  own sqlite DB (`--database-url`) so the DB itself does not conflict. If the
  shared settings become annoying, give each instance its own `--user-directory`
  and symlink only the `default/workflows` subdir to a common folder.
* The `free_gpu` idle watchdog must not fire during a single long generation
  (a WAN video can run 20+ min on one POST `/prompt`). The hook now checks the
  ComfyUI prompt queue (`_generation_in_progress`) and defers the idle countdown
  while a job is running/queued, so it never unloads a model mid-sampling.
* ComfyUI-Login is "basic protection" (its own words) — a shared password, not
  per-user isolation. If true per-user asset separation is later needed, revisit
  ComfyUI-Usgromana or one-instance-per-user.
* **`comfyui-secure` will not come up on its own after a reboot.** Its unit has
  `ConditionPathIsMountPoint=/srv/ai/storage` (a hand-installed
  `RequiresMountsFor=/srv/ai/storage` drop-in until 2026-10-15 — see that
  addendum), so at boot — while the encrypted volume is still locked (manual
  passphrase, ADR: `storage-crypt.sh`) — its start is skipped, and systemd does
  **not** re-check once the volume is later unlocked. `scripts/storage-crypt.sh
  unlock` therefore starts `comfyui-secure` as its final step (best-effort, same
  as its filebrowser recreate), so unlocking the volume also brings the secure
  instance up. The dashboard's "▶ Start" button on the secure row is the manual
  equivalent. `comfyui-open` has no encrypted dependency and comes up at boot
  normally.

## Files

* `scripts/comfyui-open.service`, `scripts/comfyui-secure.service`
* `scripts/comfyui-secure-extra-paths.yaml`
* `scripts/install-comfyui-instances.sh` (run with sudo; supersedes the old
  single `comfyui.service`)
* `scripts/reset-comfyui-password.sh` (run with sudo; deletes the bcrypt
  `login/PASSWORD` and restarts the locked instance so a new one can be set)
* Network exposure & firewall guidance: `docs/server-setup.md` — the open
  `:8188` has no auth and must stay LAN/Tailscale-only (ufw ruleset provided)
* Local (gitignored): `comfyui/custom_nodes_secure/ComfyUI-Login`,
  `comfyui/{output,input,temp}-{open,secure}`, `comfyui/user`, `comfyui/login/`

## Addendum 2026-10-15: ComfyUI-Login secret file permissions (audit)

**Finding.** ComfyUI-Login's secrets were readable by every local user, and by
any container that mounts these paths:
- The first line of `login/PASSWORD` (0644) is the bcrypt hash of the password,
  and the plugin accepts that hash as an API **bearer token**. That is how the
  secure MCP bridge authenticates (ADR-0026). So reading the file grants full
  access to the instance without knowing the password.
- On every start the plugin logs `For direct API calls, use token=<hash>`. The
  line goes to the journal and to ComfyUI-Manager's `user/comfyui_<port>.log`
  files (0644).
- `.secret-key.txt` (signs the session cookies, rotated every 30 days) sits in
  the plugin's own directory.

The Qwen instance (ADR-0024) has its own copies of all of these. It has no
ComfyUI-Manager, so it writes no log files.

**Fix (live 2026-10-15, no restarts).**
- `comfyui/login`, `comfyui-qwen/login`, `comfyui/user` and both
  `ComfyUI-Login` plugin dirs are now 0700 (owner brad). The directory modes
  are the lasting protection: the plugin recreates `PASSWORD` with the default
  umask (0644) after a reset, and the log files are recreated on every start.
  Both land inside a 0700 directory.
- The existing files are also 0600: both `PASSWORD` files, both
  `.secret-key.txt` files and the three `comfyui_8189*.log` files.
- `scripts/install-comfyui-instances.sh` now enforces these modes, and the
  `comfyui-qwen` runbook has a pre-start step (`docs/server-setup.md`).
- Same audit, same pattern: `scripts/server-status.env` (gitignored, holds the
  Open WebUI admin API key for the status banner) was 0664. It is now 0600, and
  the docs create it with `install -m 600`.

Every reader runs as brad: comfyui-secure, comfyui-qwen, comfyui-mcp-secure,
server-status and aichat-mcp-bridge. Verified afterwards: `:8189` and `:8190`
serve `/login` (200), refuse requests without a token (401) and accept the token
(200). A container running as uid 65534 with `/srv/ai` mounted can no longer read
any of these files.

**Residuals.**
- **Root containers ignore file modes.** mcpo runs as root and mounts all of
  `/srv/ai` read-only, so it can still read these files (audit B5). *(Fixed
  2026-10-15: mcpo runs as uid 10001 and can't read them. See the ADR-0007
  2026-10-15 addendum.)*
- **Journal.** Past and future `use token=` lines stay in the journal, which root
  and the `adm`/`systemd-journal` groups can read (brad is in `adm`). It is not
  world-readable. To drop the line, add `LogFilterPatterns=~use token=` to both
  units (systemd 255; needs sudo). Not done.
- **Rotation is optional.** The old tokens were readable by any local account
  until now. To rotate, reset the password: `scripts/reset-comfyui-password.sh`
  for `:8189`; for `:8190`, delete `comfyui-qwen/login/PASSWORD` and restart
  `comfyui-qwen`. The secure MCP bridge picks up the new `:8189` token by
  itself once the new password is set. *(Fixed 2026-10-15, audit B6; see the
  ADR-0026 addendum. Before that it read the token only at start and needed a
  `comfyui-mcp-secure` restart.)*
  *Owner decision (2026-10-15): no rotation. No other account has had shell
  access to the machine, so nobody else could have read the files.*
- **SMB.** The directory modes don't protect these files on the SMB share,
  because smbd serves it as brad. Since 2026-10-16 the share's `veto files`
  hides `PASSWORD`, `.secret-key.txt` and the `comfyui_*.log` files instead
  (ADR-0022 addendum, audit E8).

## Addendum 2026-10-15: secure instance stays single-GPU; storage guard moved into the unit (audit C4)

**Drift.** The installed and repo copies of `comfyui-secure.service` disagreed:
- **Repo:** dual-GPU — `CUDA_VISIBLE_DEVICES=2,1` and `--reserve-vram 2`, so
  ComfyUI-MultiGPU loaders could park the MiniMax-H3 text encoder and VAEs on
  idx1. This came from `42913f8`, which reached main through PR #45 on
  2026-10-11.
- **Installed (2026-08-27):** single-GPU (`CUDA_VISIBLE_DEVICES=2`, no reserve).
  It still listed the dropped `chat-uncensored-q4` in `FREE_GPU_KEEP`.

The encrypted-volume guard existed only as a hand-installed drop-in
(`comfyui-secure.service.d/require-storage.conf`). It was not in the repo, and
the installer did not install it.

**Decision (owner, 2026-10-15): one GPU for now.**
- The repo unit is back to `CUDA_VISIBLE_DEVICES=2` with no `--reserve-vram`,
  which is what has actually run since 2026-08-27.
- Dual-V100 component placement is a future item. Its recipe stays in
  `docs/server-setup.md`, marked not active.
- ComfyUI-MultiGPU stays installed. No saved workflow uses its loaders.

**Storage guard: `ConditionPathIsMountPoint`, not `RequiresMountsFor`.** The
unit now carries `After=srv-ai-storage.mount` and
`ConditionPathIsMountPoint=/srv/ai/storage`. Like the drop-in, this means the
instance only starts while the volume is mounted, so its `*-secure` symlinks
can't point into an empty, unencrypted mountpoint directory. The drop-in had two
costs that the condition avoids:
- **90 s boot wait.** The unit is enabled at boot, so `RequiresMountsFor` pulled
  the `noauto` mount into the boot transaction. The LUKS-backed device only
  appears after a manual unlock, so systemd waited out its 90 s device timeout
  and then logged "Dependency failed for comfyui-secure.service". `systemd-analyze`
  for the 2026-08-24 boot: `graphical.target` reached at 1 min 30 s, while
  docker was up at about 9 s. SSH is socket-activated and was not delayed.
- **Hanging starts.** Any `systemctl start comfyui-secure` while the volume was
  locked, such as a quiet-hours restore, hung for 90 s and then failed.

A start while locked is now logged as "skipped because of an unmet condition
check" and returns 0 immediately. This was checked with a transient unit: a
mounted path runs, and a non-mountpoint is skipped with rc 0 in about 10 ms.
`install-comfyui-instances.sh` deletes the legacy drop-in. Unlock still has to
start the unit itself, because systemd does not re-check a skipped condition.

**Lock path.** `storage-crypt.sh lock` now stops `comfyui-secure` and the
`filebrowser` container before it unmounts, mirroring unlock (which starts and
recreates them). Before:
- comfyui-secure kept running against dangling paths.
- filebrowser's bind mounts of the secure dirs kept the filesystem busy, so
  `cryptsetup close` would fail.

The order was mock-tested (stop, stop, umount, close; a busy umount still
aborts before close). It was not run live, because that would lock the volume.

**Apply (sudo).** Run this when the secure instance is idle. The fp16 flag in
`/run/comfyui-fp16/secure.env` survives the restart:
```bash
sudo install -m0644 /srv/ai/scripts/comfyui-secure.service /etc/systemd/system/
sudo rm /etc/systemd/system/comfyui-secure.service.d/require-storage.conf
sudo rmdir /etc/systemd/system/comfyui-secure.service.d
sudo systemctl daemon-reload && sudo systemctl restart comfyui-secure
```
`install-comfyui-instances.sh` does the same, but it restarts both instances.

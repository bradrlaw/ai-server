# ADR-0013 — Dual ComfyUI instances (open + password-locked), one per V100

* Status: Accepted
* Date: 2026-07-07
* Deciders: @bradrlaw

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

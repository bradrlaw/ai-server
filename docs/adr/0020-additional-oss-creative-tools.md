# ADR-0020: Add optional OSS creative/training tools alongside ComfyUI

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
ComfyUI is powerful but node-graph-based and intimidating for casual users.
Family members want **simpler** image-generation UIs, and people using this repo
as a build guide should be able to pick whichever front-end suits them. Separately,
there is no on-box **training** capability (LoRA / fine-tunes) yet.

Four OSS tools were selected to add:
- **[ai-toolkit](https://github.com/ostris/ai-toolkit)** (Ostris) — diffusion-model
  **training** suite (LoRA/fine-tune) with a web UI. *This is the training tool.*
- **[Fooocus](https://github.com/lllyasviel/Fooocus)** (lllyasviel) — the simplest
  "type a prompt, get an image" UI.
- **[SwarmUI](https://github.com/mcmonkeyprojects/SwarmUI)** (mcmonkey) — a friendly
  UI that itself drives a ComfyUI backend (best of both worlds).
- **[InvokeAI](https://github.com/invoke-ai/InvokeAI)** (Invoke) — a polished
  canvas/workflow app.

Constraints that shape the install:
- **Must not interfere** with the existing native stack (llama.cpp + llama-swap
  on the V100s/P100, ComfyUI on the V100s) or the Docker app tier. → each tool gets
  its **own venv** and its **own port**; none share a process or Python env.
- **Volta/Pascal + CUDA 12.x only** (ADR-0003). Modern image tools increasingly pin
  CUDA 13.x / torch ≥2.7 wheels that **drop sm_70/sm_60 kernels**. Each tool must be
  installed against a **cu124 / torch 2.6.0** build (the ComfyUI-proven one) — see
  the per-tool notes in [`../optional-tools.md`](../optional-tools.md).
- **Agent cannot sudo.** Builds/venvs are user-space; each tool ships a systemd unit
  under `scripts/` that the owner installs by hand.

## Decision
Install the four tools **natively** (venv per tool, following the same pattern as
ComfyUI), each on its **own port**, each **monitored by the `server-status` page**,
each with a **reproducible install script** in `scripts/` and docs in
`optional-tools.md`. They are **optional add-ons**: the core serving stack does not
depend on them, and a follower can install any subset.

Port map (each tool's own default where free; InvokeAI moved off 9090 which
collides with the llama-swap management port):

| Tool | Purpose | Port |
|------|---------|------|
| ai-toolkit UI | model **training** (LoRA/fine-tune) | 8675 |
| Fooocus | simple image gen | 7865 |
| SwarmUI | image gen (ComfyUI-backed) | 7801 |
| InvokeAI | image gen (canvas/pro) | 9091 |

**Model sharing with ComfyUI** (checkpoints/LoRAs/VAEs) is desirable to avoid
duplicating tens of GB, but is deliberately **deferred**: get each tool running on
its own first, then wire shared model paths. Tracked as a follow-up.

## Consequences
- Positive: non-technical users get approachable UIs; the repo becomes a
  pick-and-choose guide; on-box LoRA training becomes possible.
- Positive: isolation (own venv + port) means a broken/updated tool cannot take
  down llama-swap or ComfyUI.
- Negative / trade-offs: more venvs = more disk (mitigated by the cu124 torch being
  shared-version, and by putting cold model weights on the bulk tier); each tool
  fights the current of being built for newer GPUs, so some install pins must be
  hand-reconciled to the torch-2.6 line and the newest models may not run on Volta.
- Follow-ups / things to watch:
  - Configure shared model dirs with ComfyUI (deferred).
  - `ffmpeg` is not installed system-wide — needed for ai-toolkit **video/audio**
    training (torchcodec) and possibly other tools; hand to owner as `apt install`.
  - Each tool's own auto-updater may reintroduce a cu130 torch; pin/verify on update.

## Alternatives considered
- **Docker the new tools** — rejected for the GPU tools for the same reason as
  ADR-0006 (native GPU tiers avoid container CUDA/driver friction on Volta).
- **Only add one UI** — rejected; the whole point is to compare ease-of-use and let
  users/followers choose.
- **Use each tool's bundled installer as-is** — rejected: they pull cu130/torch-2.13
  wheels with no sm_70 kernels, which cannot run on this hardware.

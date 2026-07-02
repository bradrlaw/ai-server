# ADR-0001: Keep all work under `/srv/ai`

- **Status:** Accepted
- **Date:** 2026-06-30
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The server hosts multiple AI-related workloads (inference engines, models,
datasets, TTS/STT, scripts, docs). A single, predictable root keeps the project
self-contained, easy to back up, and easy to reason about across sessions.

## Decision
All project artifacts live under **`/srv/ai`**, organized by purpose:

```
/srv/ai/
  comfyui/   datasets/   docker/    docs/      models/
  pkg/       scripts/    src/       tts/       whisper/
  copilot/   (Copilot CLI working dir)
```

- Setup/maintenance scripts -> `/srv/ai/scripts/`
- Documentation & decisions -> `/srv/ai/docs/` (ADRs in `/srv/ai/docs/adr/`)
- Source checkouts (e.g. llama.cpp) -> `/srv/ai/src/`
- Model weights -> `/srv/ai/models/`

## Consequences
- Positive: predictable layout; simple backup scope; portable.
- Negative: `/srv/ai` sits on the 2TB root NVMe — large model growth must be
  watched (see server-setup.md §9). Consider a dedicated models mount if it fills.
- Follow-up: keep this convention for every new artifact.

## Alternatives considered
- **Home directory (`~`)** - rejected; less clear for a multi-purpose server and
  couples project data to a single user account.
- **`/opt`** - reasonable, but `/srv` better matches "data/services served by
  this host" and is already adopted.

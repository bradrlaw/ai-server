# Hermes — custom skills

The Hermes agent gateway (`docker/docker-compose.yml`, container `hermes`) keeps all of
its runtime state under `/srv/ai/hermes` (bind-mounted to `/opt/data`), which is **local &
gitignored** — skills, the kanban board data, config, everything. Most of what lives under
`hermes/skills/` is bundled with the Hermes image itself (research templates like `arxiv`,
`polymarket`, `research-paper-writing`, etc.) and isn't ours to track.

This directory holds the **tracked, reproducible snapshot** of the custom skills written
for this server — currently just one:

| Skill | Live path | What it does |
|-------|-----------|---------------|
| `v100-performance-scout` | `/srv/ai/hermes/skills/research/v100-performance-scout/SKILL.md` | Weekly-cron research skill that scans the web for new llama.cpp/ComfyUI techniques, quantization formats, and tuning tips relevant to this server's Tesla V100 (Volta, `sm_70`) GPUs, filters out anything that doesn't apply to Volta (fp8, FlashAttention-2, CUDA 13+), and files a single reviewable kanban card per run on the `v100-perf-scout` board. Observe-and-report only — never changes server config directly. Surfaced on the status dashboard's "Latest V100 findings" card (`scripts/server-status-service.py`). |

To deploy a change: edit the file under `config/hermes/skills/...` here, then copy it to
the matching live path under `/srv/ai/hermes/skills/...` (no restart needed — Hermes reads
skills from disk per-invocation). Keep both copies in sync; this tracked copy is the
source of truth for history/review, the live one is what the agent actually executes.

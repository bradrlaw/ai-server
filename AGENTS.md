# AGENTS.md — orientation for AI coding agents

Personal **headless AI server** (`/srv/ai`) serving local LLMs, speech-to-text,
embeddings/RAG, image/video generation, and long-running agents behind one
OpenAI-compatible API, for the owner (@bradrlaw) and family.

Read this first, then the detailed docs:
- **[README.md](README.md)** — human-facing overview.
- **[docs/architecture.md](docs/architecture.md)** — serving topology, GPU tiering, phased rollout (§6).
- **[docs/server-setup.md](docs/server-setup.md)** — the runbook: build, benchmarks, fan/power, services, ComfyUI.
- **[docs/adr/](docs/adr/)** — Architecture Decision Records. **Record notable decisions here as new ADRs.**

## Repo scope & conventions
- **This repo tracks docs + scripts + config only.** `.gitignore` is a **whitelist**:
  only `docs/`, `scripts/`, `docker/`, `config/`, `README`, `LICENSE` are tracked.
  Everything else under `/srv/ai` (`models/`, `venvs/`, `src/`, `comfyui/`, `datasets/`) is **local & gitignored** — do not try to commit it.
- **Keep all work under `/srv/ai`.** Record design decisions as ADRs in `docs/adr/`.
- **Commits:** append the trailer `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.
- Remote: `github.com/bradrlaw/ai-server` (private).

## Critical operational facts (avoid these traps)
- **Agents cannot `sudo`.** Anything privileged (systemd install/restart, apt) must be
  handed to the user as a script/command they run. Non-privileged docker + llama-swap ops are fine.
- **GPU ordering gotcha:** CUDA/llama.cpp/PyTorch order GPUs by *speed* by default
  (V100s first, P100 last) — the OPPOSITE of `nvidia-smi`. **Always export
  `CUDA_DEVICE_ORDER=PCI_BUS_ID`** for pinning/benchmarks. llama-swap model blocks already set it.
- **Hardware:** i7-6950X, 128 GB RAM, 2 TB NVMe. GPUs: 2× Tesla V100-32GB (sm_70, idx1/idx2)
  + 1× Tesla P100-16GB (sm_60, idx0), **no NVLink** (PCIe PHB). Ubuntu 24.04, driver 580, CUDA 12.x.
- **CUDA 12.x only** — 13.x dropped Pascal/Volta (sm_60/sm_70). sm_70 also means **no fp8 /
  FlashAttention-2 / SageAttention**; use `sdpa` attention and avoid `*_fast` fp8 paths in ComfyUI.
- **Keep GPUs under thermal limits** via power caps applied at boot by the `gpu-fan-control`
  service (P100 200W, V100s 175W); V100 HBM throttles ~85 °C.
- **Clock is UTC, owner is US Eastern.** The machine runs `Etc/UTC` but the owner thinks in
  EST/EDT (~4–5 h offset). Any wall-clock scheduling (cron, timers, the quiet-hours window) must
  be timezone-aware — e.g. quiet-hours reads `QUIET_TZ=America/New_York`, not the system clock.
- **HF downloads:** use `/srv/ai/scripts/hf-dl download <repo> <path> --local-dir <dir>`
  (injects token from keyring, ~150 MB/s Xet). Don't use raw curl (throttled) or `HF_XET_HIGH_PERFORMANCE=1`.

## Serving stack (hybrid deploy — ADR-0006)
- **GPU/low-level tiers run NATIVE** (systemd/venv): llama.cpp + **llama-swap** router,
  ComfyUI, (future) vLLM/whisper.
- **CPU app tier runs in Docker Compose** (`docker/`): **LiteLLM** (client-facing gateway
  `:4000`), Open WebUI, Qdrant, Postgres, MCPO, SearXNG, etc.
- **Model router:** `config/llama-swap.base.yaml` is the **canonical** source (matrix router,
  mgmt `127.0.0.1:9090`); the active `config/llama-swap.yaml` the service reads is **generated**
  from the base + a mode overlay (`config/modes/*.yaml`) by `scripts/llama-swap-mode.py` and is
  **gitignored**. Edit the **base**, then re-render with `llama-swap-mode.py set <mode>`.
  **LiteLLM (`docker/litellm/config.yaml`) must have a matching `model_list` entry for every model
  you want clients to see;** restart the `litellm` container after edits.
- **Serving modes (ADR-0015):** `daily` (balanced, all single-slot), `heavy-coding` (interactive
  `coding` at full ctx + `chat`/`fast` sub-agent pools at `--parallel 4`), and `agentic`
  (autonomous throughput: `coding` P=2, `gemma-26b` P=8 in place of `chat` on idx2, `fast` P=2).
  Switch with `scripts/llama-swap-mode.py set <mode>` (no restart — `-watch-config` reloads) or the
  `llama-swap-mode` MCP (`set_mode`). `list` / `current` / `show <mode>` to inspect.
- **Model roster** (see `config/llama-swap.base.yaml` for exact args): `coding` (Qwen3.6-27B Q6_K,
  160k ctx, idx1), `chat` (Qwen3.6-35B-A3B MoE, idx2), `big` (27B BF16, dual-V100), `fast`
  (Gemma-4-12B, P100, non-reasoning), `gemma-31b`/`gemma-26b` (comparison), `chat-uncensored-q4/q6`.
  Most Qwen3.6 models are **reasoning** models (thinking phase) — give generous `max_tokens`.

## Testing a config change
- llama-swap runs with `-watch-config` (auto-reloads YAML). Edit `config/llama-swap.base.yaml`,
  validate with `python3 -c "import yaml; yaml.safe_load(open('config/llama-swap.base.yaml'))"`,
  re-render with `python3 scripts/llama-swap-mode.py set <current-mode>`, then load a model via
  `curl 127.0.0.1:9090/v1/chat/completions` and check fit with `nvidia-smi`.
- After testing, **restore the daily models** (`coding` + `chat` + `fast`) with
  `scripts/llama-swap-mode.py set daily` (warms the trio).

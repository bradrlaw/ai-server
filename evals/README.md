# Model evals

A set of **repeatable prompts** for comparing models on the AI server. Each eval is a
fixed prompt plus a scoring rubric; we run it against multiple models and keep every
model's raw output so results are reproducible and diffable over time.

## Layout

```
evals/
  README.md                     # this file
  <test-name>/                  # short, descriptive kebab-case name
    README.md                   # what the test measures, the rubric, how to run
    prompt.txt                  # the exact prompt sent to every model (verbatim)
    check.py                    # optional: objective/automatable scoring for this test
    outputs/
      <model-label>/            # one directory per model run
        index.html | output.*   # the model's extracted output
        raw.txt                 # full raw reply (incl. any reasoning_content)
        meta.json               # model, load command, sampler, usage, MTP, perf timings
        run.html                # human-readable view: metrics + rendered output + raw
        check.json              # optional: objective-check results (from check.py)
        scores.json             # manual design scores (five 0–5 axes + notes)
    summary.html                # auto-built comparison table across all runs
    RESULTS.md                  # auto-built scoreboard (objective+perf + manual scores)
```

Conventions:
- **`prompt.txt` is verbatim and frozen.** Changing it invalidates prior outputs — start a
  new test dir (or a versioned name) instead of editing a prompt that already has results.
- **One directory per model run**, named by a short model label (e.g. `coding`,
  `thinkingcap-27b`, `gemma-31b`). Re-running overwrites that model's dir.
- **Keep raw output.** `raw.txt` + `meta.json` make every run auditable and reproducible.
- **`scores.json` is the only hand-edited result file** — the manual design scores. A blank
  stub is auto-created per run; fill it and rebuild. `summary.html` and `RESULTS.md` are
  generated, never hand-edited.
- **`summary.html` is generated, never hand-edited.** `eval-run.py` refreshes it after every
  run; rebuild it from scratch anytime with `scripts/eval-summary.py --test <name>`.

## Running a test

The runner sends `prompt.txt` to an OpenAI-compatible endpoint, extracts the primary code
block from the reply, and writes `outputs/<label>/`:

```bash
# Against a llama-swap-routed model (loads on demand via the router on :9090):
scripts/eval-run.py --test localmind-landing-page --model coding
scripts/eval-run.py --test localmind-landing-page --model gemma-31b --temp 0.7

# Against a standalone llama-server (e.g. a candidate not wired into llama-swap):
scripts/eval-run.py --test localmind-landing-page --model thinkingcap \
    --endpoint http://127.0.0.1:8902/v1/chat/completions --label thinkingcap-27b
```

Key flags: `--label` (output dir name), `--endpoint`, `--temp/--top-p/--top-k`,
`--max-tokens` (default 32000 — code output can be long), `--ext` (default `html`),
`--cmd` (record a standalone server's launch command). `scripts/eval-run.py --help` for
all options. Reasoning models put their thinking in `reasoning_content`; only the answer
(`content`) is used for the output file, but the full reply is preserved in `raw.txt`.

### Agent-mediated runs (Copilot CLI / BYOK)

`scripts/eval-run.py` measures the **raw model** (one verbatim user message, one shot).
To instead measure the model **plus a coding-agent scaffold** — GitHub Copilot CLI's
system prompt and multi-turn tool loop, driving the *same local weights* via BYOK
(`copilot-byok.sh` → LiteLLM `:4000`) — use the sibling runner. It works for **any**
test, reuses the same `prompt.txt`, `check.py`, and scoreboard, and lands under a
distinct label (default `<model>-copilot`) so it sits beside the raw run:

```bash
scripts/eval-run-copilot.py --test local-dungeon-web --model coding   # -> coding-copilot
scripts/eval-run-copilot.py --test dungeon-adventure-engine --model chat --ext py
```

It runs `copilot -p` headless in an isolated temp dir (`--allow-all-tools --no-ask-user
--no-custom-instructions`), disables the plan-build MCP so the selected model stays
pinned (no planner→coder model swap), applies the per-model token budgets from
`copilot-byok.sh`, and captures whatever file the agent writes. Agent runs record
`harness: github-copilot-cli` in `meta.json` and leave the perf/token columns blank
(the endpoint metrics aren't exposed through the CLI). Compare raw vs `-copilot`
side-by-side to isolate the harness effect; don't average the two — they answer
different questions ("how good is the model" vs "how good is model + tool").

Each run writes `outputs/<label>/` (`index.html`, `raw.txt`, `meta.json`, `run.html`) and
refreshes the test's `summary.html`.

### What gets recorded (`meta.json`)

- **Identity:** `model_slot` (the llama-swap model id requested), `model_name` /
  `model_path` (the actual GGUF served), `endpoint`, `proxy`.
- **`load_command`** — the *exact* llama.cpp launch command (fully expanded, read from the
  llama-swap router's `/running`; for a standalone server pass `--cmd`).
- **`mtp`** — `enabled` plus draft-token `accept_rate` when MTP self-speculative decode is on.
- **`sampler`** — the request-side params (temperature/top_p/top_k/max_tokens).
- **`performance`** — server-side llama.cpp `timings` (network-independent):
  - `ttft_ms` — time to first token = prefill (`prompt_ms`)
  - `prefill_tps` — prompt tokens/sec (`prompt_per_second`)
  - `decode_tps` — output tokens/sec (`predicted_per_second`)
  - plus `wall_secs` (total run time) and the raw `server_timings`.

  Runs force a **cold prefill** (`cache_prompt: false`) so TTFT / prefill numbers are real
  and comparable across models instead of reflecting a warm KV cache.

### Viewing results

- **`summary.html`** (test root) — one sortable table comparing every run on objective
  score, output size, TTFT, prefill/decode throughput, MTP, and wall time. Rows link to
  each run's page and output. Rebuild anytime:

  ```bash
  scripts/eval-summary.py --test localmind-landing-page   # one test
  scripts/eval-summary.py --all                           # every test
  ```

- **`outputs/<label>/run.html`** — a per-run page: metric cards, the llama.cpp load
  command, the sampler, the full `meta.json`, and (for HTML tests) the rendered page inline.

Both are plain files. **To view rendered pages, use the evals web viewer at
`http://<server>:8085/`** (a read-only static server that serves this tree with
directory listing) — open `localmind-landing-page/summary.html`, then click into any
run. Filebrowser (`:8083` → `/data/evals`) is fine for downloading, but it shows HTML
*source* rather than rendering it.

## Scoring

Each test defines its own rubric in its `README.md`. Two parts:

- **Objective (automated)** — `check.py` scores objectively-checkable constraints
  (self-containment, required sections/interactions) and writes `check.json` per run.
- **Subjective (manual)** — design/quality axes scored 0–5 by hand. Enter them in each run's
  `outputs/<label>/scores.json` (a blank stub is auto-created), then rebuild:

  ```bash
  scripts/eval-summary.py --test <name>
  ```

  This regenerates **`RESULTS.md`** — the scoreboard merging the automated objective/perf
  metrics with your design scores — and refreshes the **Design** column in `summary.html`
  and the axis cards in each `run.html`. `RESULTS.md` and `summary.html` are generated;
  edit `scores.json`, not them.

- **Playability (headless, optional)** — for interactive HTML tests, `check.py` only proves
  constraints are *present*, not that the game is *finishable*. A test can add a
  `playtest.json` scenario and drive each generated `index.html` in a headless
  [jsdom](https://github.com/jsdom/jsdom) DOM via the game-agnostic harness:

  ```bash
  npm --prefix scripts/eval-playtest install     # one-time (jsdom)
  node scripts/eval-playtest/playtest.js --test <name>
  ```

  It writes `outputs/<label>/playtest.json` + a `PLAYTEST.md` milestone report. This is a
  **separate signal — NOT folded into the `check.py` score.** See `local-dungeon-web` for a
  worked example (it catches games that score full marks yet seal off the Secret Chamber).

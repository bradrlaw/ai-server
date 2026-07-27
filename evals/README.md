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
    summary.html                # auto-built comparison table across all runs
    RESULTS.md                  # optional: summary scoreboard across models
```

Conventions:
- **`prompt.txt` is verbatim and frozen.** Changing it invalidates prior outputs — start a
  new test dir (or a versioned name) instead of editing a prompt that already has results.
- **One directory per model run**, named by a short model label (e.g. `coding`,
  `thinkingcap-27b`, `gemma-31b`). Re-running overwrites that model's dir.
- **Keep raw output.** `raw.txt` + `meta.json` make every run auditable and reproducible.
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

Both are plain files — browse them in Filebrowser (`:8083` → `/data/evals`) or open directly.

## Scoring

Each test defines its own rubric in its `README.md`. Where constraints are objectively
checkable (self-containment, required sections, required interactions), a `check.py`
automates that portion; subjective quality (visual design, code quality) stays a manual
score. Record final scores in the test's `RESULTS.md`.

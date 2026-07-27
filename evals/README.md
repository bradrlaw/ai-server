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
        meta.json               # model, endpoint, sampler, tokens, timing, timestamp
        check.json              # optional: objective-check results (from check.py)
    RESULTS.md                  # optional: summary scoreboard across models
```

Conventions:
- **`prompt.txt` is verbatim and frozen.** Changing it invalidates prior outputs — start a
  new test dir (or a versioned name) instead of editing a prompt that already has results.
- **One directory per model run**, named by a short model label (e.g. `coding`,
  `thinkingcap-27b`, `gemma-31b`). Re-running overwrites that model's dir.
- **Keep raw output.** `raw.txt` + `meta.json` make every run auditable and reproducible.

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
`--max-tokens` (default 32000 — code output can be long), `--ext` (default `html`).
`scripts/eval-run.py --help` for all options. Reasoning models put their thinking in
`reasoning_content`; only the answer (`content`) is used for the output file, but the full
reply is preserved in `raw.txt`.

## Scoring

Each test defines its own rubric in its `README.md`. Where constraints are objectively
checkable (self-containment, required sections, required interactions), a `check.py`
automates that portion; subjective quality (visual design, code quality) stays a manual
score. Record final scores in the test's `RESULTS.md`.

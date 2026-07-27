# Eval: LocalMind landing page (single-file HTML)

A **one-shot front-end coding test**: ask the model to produce a complete, polished,
*self-contained* dark-mode product landing page in a single HTML file with everything
inline (HTML + CSS + JS), zero external dependencies, real interactions, and full
responsiveness. It stresses instruction-following under many hard constraints, front-end
breadth (layout, responsive CSS, vanilla JS interactivity), and design taste in one turn.

The exact prompt is in [`prompt.txt`](prompt.txt) and is sent **verbatim** to every model.

## How to run

```bash
# One model via the llama-swap router:
scripts/eval-run.py --test localmind-landing-page --model coding

# A standalone candidate server (e.g. ThinkingCap on :8902):
scripts/eval-run.py --test localmind-landing-page --model thinkingcap \
    --endpoint http://127.0.0.1:8902/v1/chat/completions --label thinkingcap-27b
```

Output lands in `outputs/<label>/` (`index.html`, `raw.txt`, `meta.json`). Open the HTML
directly in a browser (it must work from `file://` with no network) and resize from 320px
to 1440px+ to judge responsiveness.

## Scoring

### Objective (automated) — `check.py`

Encodes the prompt's *hard constraints* as pass/fail checks (self-containment, required
sections, required interactions) and prints a weighted score:

```bash
evals/localmind-landing-page/check.py            # scores every outputs/<label>/index.html
```

It writes `check.json` next to each output and reports a `score/max` (currently 27 pts).
The single most important check is **`self_contained`** — any external URL in `src`/`href`/
`@import`/`url()` violates the core "zero external dependencies / one file" constraint.

### Subjective (manual) — 0–5 each

`check.py` cannot judge taste. Score these by opening the page:

| Axis | What to look for |
|---|---|
| **Visual polish** | Linear/Vercel/Raycast energy: spacing, type scale, subtle borders/glows, restraint |
| **Responsiveness** | Genuinely good from 320px → 1440px+, mobile-first; no overflow, readable hero |
| **Interaction quality** | Hamburger opens/closes smoothly; hover/active states feel alive; scroll entrance animations are tasteful, not janky |
| **Hero visual** | The CSS-only animated element (orb/pulse/gradient) actually looks intentional |
| **Code quality** | Readable, well-structured, no dead/placeholder styles, semantic HTML |

Record final numbers in `RESULTS.md` (objective score + the five manual axes + notes).

## Notes on fairness

- **Sampler:** default run uses `temp 0.7, top_p 0.95, top_k 20`. Record any per-model
  deviation in that model's `meta.json` (the runner already captures the sampler).
- **max_tokens:** a full page is long; the runner defaults to 32000. If `meta.json` shows
  `finish_reason: "length"`, the page was truncated — bump `--max-tokens` and re-run.
- **Extraction:** the model is told to output only a code block; the runner extracts the
  fenced ```` ```html ```` block. If a model wraps prose around it, `raw.txt` keeps the
  full reply and `meta.json.extraction` records how the code was recovered.

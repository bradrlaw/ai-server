# local-dungeon-web

A single-turn **front-end coding** test: build the same six-room text adventure
as [`dungeon-adventure-engine`](../dungeon-adventure-engine/), but as a
**completely self-contained browser game** — one `.html` file with HTML, CSS, and
game logic all inline, zero external dependencies, a playable dark-mode terminal
UI, and a live sidebar dashboard. It stresses instruction-following under many
hard constraints, ES6 architecture, front-end breadth (responsive layout,
DOM-driven UI, keyboard interactions), and design taste in one output.

The web analog of the Python-CLI `dungeon-adventure-engine` (same lore/rooms/items,
different deliverable — like `localmind-landing-page` vs its variants).

The exact prompt is in [`prompt.txt`](prompt.txt) and is sent **verbatim** to
every model.

## The prompt asks for

- **One self-contained `.html`:** inline `<style>` + `<script>`, system fonts,
  inline SVG icons, zero CDNs/frameworks/external fonts.
- **Responsive layout:** sticky header (title + tagline + "Reset Game"), lore
  intro, a collapsible "How to Play" accordion, and a 70/30 two-column dashboard
  (game console left, stats/inventory sidebar right) that stacks on mobile.
- **ES6 architecture:** `GameEngine`, `Room`, `Item` classes; state for current
  room, inventory, visited rooms, and boolean flags (e.g. `lever_pulled`).
- **Robust parser:** case-insensitive, synonyms (`examine`=`look at`,
  `grab`=`take`), partial matches (`n`→north, `inv`→inventory), graceful errors.
- **The dungeon:** Entrance Hall (rusted key), Library (lever → secret chamber),
  Armory (torch, locked north door needs the key), Treasure Room (golden coin),
  Secret Chamber (ancient scroll), Garden.
- **Interactions:** typewriter output with skip, Up/Down command history,
  auto-scroll, click-to-focus input, live inventory/stats updates, and a reset
  that clears state **without** refreshing the page.
- **Design:** dark mode (`#1a1a1a`), green/amber console (`#00ff00` / `#ffb000`),
  monospace console + system-ui/sans-serif UI.

## How to run

```bash
# One model via the llama-swap router:
scripts/eval-run.py --test local-dungeon-web --model coding

# A standalone candidate server:
scripts/eval-run.py --test local-dungeon-web --model thinkingcap \
    --endpoint http://127.0.0.1:8902/v1/chat/completions --label thinkingcap-27b

# Agent-mediated (same weights via GitHub Copilot CLI / BYOK, label <model>-copilot):
scripts/eval-run-copilot.py --test local-dungeon-web --model coding
```

Output lands in `outputs/<label>/` (`index.html`, `raw.txt`, `meta.json`,
`run.html`) and the run is added to `summary.html` at the test root. Open
`index.html` directly in a browser — it must work from `file://` with no network —
play a few commands, and resize from 320px to 1440px+ to judge responsiveness.

## Scoring

### Objective (automated) — `check.py`

Encodes the prompt's *hard constraints* as pass/fail checks and prints a weighted
score:

```bash
evals/local-dungeon-web/check.py        # scores every outputs/<label>/index.html
```

It writes `check.json` next to each output and reports a `score/max`. Groups:
self-containment (the single most important is **`self_contained`** — any external
URL violates "one file, zero dependencies"), page layout (sticky header, reset
button, How-to-Play accordion, responsive `@media`, two-column dashboard, stats +
"Inventory is empty"), ES6 architecture (`GameEngine`/`Room`/`Item` classes,
case-insensitivity, synonyms, partial matches, state flags), the six rooms + five
items, interactions (typewriter, Up/Down history, auto-scroll, click-to-focus,
reset without reload), and dark-mode design tokens.

### Subjective (manual) — 0–5 each (`scores.json`, see `rubric.json`)

`check.py` verifies presence, not quality. Score these by opening and *playing*
the page:

| Axis | What to look for |
|---|---|
| **Architecture** | Clean `GameEngine`/`Room`/`Item` separation; sensible data model for exits/locks/items; state cleanly centralized; no tangled globals |
| **Completeness** | Every room, item, exit, synonym, partial match, and the lever→secret-chamber / key→treasure-door logic actually implemented and wired to the UI |
| **Parser robustness** | Graceful messages for unknown commands, missing items, locked doors; partial/synonym resolution feels natural; no dead-ends or crashes |
| **Interaction/UX** | Typewriter with working skip, Up/Down history, auto-scroll, click-to-focus, live inventory/stats, in-place reset — all smooth and responsive 320px→1440px+ |
| **Visual polish** | Convincing retro-terminal + modern sidebar; restrained dark palette; good spacing/type; inline SVG icons look intentional |

Enter scores in each `outputs/<label>/scores.json` (a blank stub is auto-created),
then run `scripts/eval-summary.py --test local-dungeon-web` to regenerate the
scoreboard. `RESULTS.md` and `summary.html` are generated — don't hand-edit them.

### Playability (headless) — `playtest.json` → `PLAYTEST.md`

`check.py` verifies that constraints are *present*, not that the game is
*playable* — a model can score full marks yet ship a game you can't finish. The
canonical example here is the **wall lever**: it's a fixture that opens the
Secret Chamber, so pulling it should NOT require picking it up first. Several
outputs get this wrong (you must `take lever` before `use lever`), and one
(`coding`) actually *corrupts state* if you take it, sealing the Secret Chamber —
all while scoring 41/41 objectively.

To catch this, a headless [jsdom](https://github.com/jsdom/jsdom) harness loads
each `index.html` in a fake DOM, drives the parser through real command
sequences, and reports whether the Secret Chamber is reachable two ways:

```bash
npm --prefix scripts/eval-playtest install      # one-time (jsdom)
node scripts/eval-playtest/playtest.js --test local-dungeon-web
```

It plays two scenarios per output — **Fixture (no take):** `north` → `use lever`
→ `west`, and **Take-first workaround:** the same with a `take lever` inserted —
and writes `outputs/<label>/playtest.json` plus a human-readable `PLAYTEST.md`
with per-command milestones. This is a **separate playability signal — it is NOT
folded into the `check.py` score.** A UI the driver genuinely can't drive shows
`✗` without affecting the objective number.

The driver is deliberately game-agnostic: it discovers the console element by
tracking which container *grows* after each command (so it ignores inline
`<script>` source and static lore), polyfills `innerText` (which jsdom otherwise
doesn't reflect) for char-by-char typewriters, waits out the typewriter before
reading, and tries alternate movement phrasings (`north` vs `go north`) only when
a command is genuinely unrecognized.

## Notes on fairness

- **Sampler:** default run uses `temp 0.7, top_p 0.95, top_k 20`. The runner
  records any per-model deviation in that model's `meta.json`.
- **max_tokens:** a full game is long; the runner defaults to 32000. If
  `meta.json` shows `finish_reason: "length"`, bump `--max-tokens` and re-run.
- **Extraction:** the model is told to output only a code block; the runner
  extracts the fenced ```` ```html ```` block. `raw.txt` keeps the full reply and
  `meta.json.extraction` records how the code was recovered.

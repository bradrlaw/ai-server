# dungeon-adventure-engine

A from-scratch **coding** test: implement a complete text-adventure game engine
in a single, standard-library-only Python file — classes, a command parser, and a
playable six-room dungeon. It stresses architecture, breadth (many small
requirements that must all be satisfied), and code quality over a long output.

## The prompt

Verbatim in [`prompt.txt`](prompt.txt). It asks for:

- **Core classes:** `GameState` (room, inventory, visited, flags, history),
  `Room` (exits, items, locked doors), `Item` (name, description, `on_use`
  callback), `GameEngine` (parse → route → mutate → output).
- **Commands:** `look`, `look at <item>`, `go/move/<direction>`, `take`, `drop`,
  `inventory`/`i`, `use`, `help`, `history`, `quit` — case-insensitive, with
  partial matches (`inv`→inventory, `n`→north), synonyms (`examine`=look at,
  `grab`=take), and graceful errors.
- **A built-in dungeon:** Entrance Hall, Library (lever reveals a secret
  passage), Armory (locked north door needs the rusted key), Treasure Room,
  Secret Chamber (scroll = secret ending, sets a `won` flag), Garden. Five items
  with specific behaviors.
- **Quality bar:** type hints, docstrings, a `__main__` game loop, graceful
  `EOFError`/`KeyboardInterrupt` handling, ASCII banner, no global mutable state.

## How to run

Output is Python, so extract a `py` block:

```bash
scripts/eval-run.py --test dungeon-adventure-engine --model <model> --ext py
evals/dungeon-adventure-engine/check.py       # static + dynamic -> check.json
scripts/eval-summary.py --test dungeon-adventure-engine
```

## Scoring

**Objective (automated, `check.py`).** Combines **static** analysis (AST + source
scan) with a **dynamic** smoke test that actually runs the program:

- *Static:* parses; the four required classes; stdlib-only imports; type-hint
  coverage (≥50% of functions); docstring coverage (≥40% of classes/functions);
  `__main__` block; `EOFError` + `KeyboardInterrupt` handling; command vocabulary
  (≥8 of the 9 verbs); the six rooms; the five items; ASCII banner; the `won`
  flag; lever/secret-passage logic.
- *Dynamic:* the file is run in a subprocess with a scripted session
  (`look → take rusted key → inventory → look → quit`) under a 20s timeout — it
  must start, print the starting room, exit cleanly, and reflect the taken item
  in the inventory.

Max 29. The generated program is executed (self-contained stdlib code from a
local model; no network, killed on hang).

**Code quality (manual, `scores.json`, 0–5 each — see `rubric.json`):**

- **Architecture** — clean separation of GameState/Room/Item/GameEngine; no global mutable state; sensible data model for exits/locks/items.
- **Code quality** — naming, readability, type-hint/docstring quality (beyond mere presence), no dead code.
- **Completeness** — every command, synonym, partial-match, item behavior, and room actually implemented and wired up.
- **Robustness** — graceful errors for bad commands/absent items/locked doors; clean EOF/interrupt handling; no crashes mid-session.
- **Output polish** — banner, separators, locked-door messaging, victory/secret-ending presentation.

Enter scores in each `outputs/<label>/scores.json`, then rerun
`scripts/eval-summary.py --test dungeon-adventure-engine`. `RESULTS.md` /
`summary.html` are generated — don't hand-edit.

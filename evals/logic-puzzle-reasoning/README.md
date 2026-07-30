# logic-puzzle-reasoning

A constraint-satisfaction ("logic grid") puzzle that tests step-by-step
deduction — **and whether a model will admit when a puzzle can't be solved.**

## The twist

This puzzle is **deliberately over-constrained: it has no valid solution.** A
brute-force search over all 5!³ = 1,728,000 assignments finds **zero** consistent
arrangements. The prompt explicitly invites this: *"If any clue is contradictory
or the puzzle has multiple solutions, say so explicitly."*

The intended correct answer is therefore **to detect and declare the
contradiction**, not to fabricate a plausible-looking table. The forcing chain:

1. Ada = Computer Vision (clue 1) and Clara = NLP (clue 11).
2. Clue 3 bars Ben from Theory and Robotics; CV and NLP are taken ⇒ **Ben =
   Reinforcement Learning.**
3. Clue 3 bars Ben from ACL and EMNLP; Ada holds NeurIPS (clue 1) ⇒ Ben is at
   **ICML or ICLR**.
4. ICLR is the Robotics paper (clue 5) ≠ RL ⇒ Ben must be at **ICML**.
5. But ICML is in **March** (clue 12) while Ben's conference is in **August**
   (clue 8). **Contradiction — no assignment survives.**

(This makes the test a strong discriminator: weaker models confidently emit a
full, wrong solution table; stronger models catch the clash.)

## How to run

Because the answer is prose (not a code block), run with `--no-extract` so the
model's full reply is saved verbatim, and `--ext md`:

```bash
scripts/eval-run.py --test logic-puzzle-reasoning --model <model> --ext md --no-extract
evals/logic-puzzle-reasoning/check.py
scripts/eval-summary.py --test logic-puzzle-reasoning
```

## Scoring

**Objective (automated, `check.py`).** Scored on the model's *visible* answer
(the saved output, not hidden `reasoning_content`):

| weight | check | what it rewards |
| ---: | --- | --- |
| 4 | `contradiction_flagged` | explicitly says there is no solution / it's contradictory |
| 3 | `conflict_pinpointed` | names the actual clash (Ben→ICML=March vs Aug, or ICLR=Robotics vs Ben=RL) |
| 1 | `ben_is_rl` | deduces Ben = Reinforcement Learning |
| 1 | `clara_june` | deduces Clara (NLP) is in June |
| 1 | `stepwise` | shows stepwise reasoning, not just an answer |
| 1 | `cites_clues` | references the clues while reasoning |
| 1 | `verifies` | attempts to verify against the clues |

Max 12. A model that hallucinates a full solution and never notices the clash
caps out around 5/12 (the intermediate-deduction points); catching the
contradiction is worth the majority of the score.

**Reasoning quality (manual, `scores.json`, 0–5 each — see `rubric.json`):**

- **Correctness** — did it reach the right conclusion (contradiction), and are its intermediate deductions sound?
- **Rigor / stepwise** — genuine, ordered deductions vs. hand-waving or guessing.
- **Contradiction handling** — how cleanly/early it isolates and explains the clash (vs. stumbling into it, or forcing a table anyway).
- **Clarity** — readability of the write-up; clean structure/tables where used.
- **Verification** — does it actually re-check its reasoning against the clues?

Enter scores in each `outputs/<label>/scores.json`, then rerun
`scripts/eval-summary.py --test logic-puzzle-reasoning`. `RESULTS.md` /
`summary.html` are generated — don't hand-edit.

# Multipass — multi-model cross-checking plan/build agent (brainstorm)

> Status: **brainstorm / exploration** (branch `multipass`). Nothing decided yet.
> This is a thinking document, not an ADR. When we commit to an approach we'll
> distil the decisions into ADR(s) and a real spec.

## 1. The idea in one paragraph

Take a large initial prompt, generate a plan, and build it out over a long
plan→build loop. The twist: **at every meaningful step, two or more *different*
models independently do the work, then check each other** — pick the best
candidate, merge the good parts, or critique-and-revise. It's the same instinct
as Copilot's rubber-ducking, but applied continuously and structurally across the
whole build, not as a one-off review. The bet: **cheap local compute spent on
consistent cross-checking can close the "one-shot" quality gap with frontier
models.**

## 2. Why this might work (the thesis)

- **Frontier models are expensive per token; our local models are ~free** (just
  electricity + our own GPUs). So the economically rational move on *this* box is
  to **trade lots of extra tokens for quality** — exactly what an ensemble does.
  A frontier API won't 5–10× its own inference to self-check on your behalf; we
  can.
- **Uncorrelated errors cancel.** If model A and model B fail in *different* ways
  (different base, training data, quantization), the probability they're *both*
  wrong *and agree* is much lower than either being wrong alone. Diversity is the
  whole game — an ensemble of near-identical models buys little.
- **Verification is often easier than generation.** "Does this compile / pass the
  test / satisfy the spec?" is a cheaper, more reliable signal than "write the
  correct thing first try." Multipass leans on that asymmetry.
- **Error compounding is the killer of long agent runs.** A 3% per-step error rate
  over 40 steps ≈ only ~30% chance of a clean run. Cutting per-step error even
  modestly (via cross-checking) has an *exponential* payoff over a long plan.

Prior art to borrow from (not copy): self-consistency / majority voting,
best-of-N + reranking, LLM-as-judge (with its known biases), debate/critique
loops, generator–verifier / actor–critic, mixture-of-agents, and Copilot's own
rubber-duck subagent.

## 3. Core loop (strawman)

```
decompose(prompt) -> plan (task DAG)
for each task node (respecting deps):
    context    = gather(repo state, plan, task, prior results)
    candidates = [ worker_k.generate(context) for k in workers ]     # 2+ models
    survivors  = objective_gate(candidates)     # compile/test/lint in isolation
    if len(survivors) == 0:  critique -> regenerate (bounded) or escalate
    if len(survivors) == 1:  winner = survivors[0]
    else:                    winner = arbitrate(survivors)  # judge / vote / merge
    apply(winner); verify(); commit();  update(plan, memory)
checkpoint after each node; resumable.
```

The interesting design surface is in `arbitrate()`, `objective_gate()`, who the
`workers` are, and **when** to spend the multi-model budget vs. run solo.

## 4. Orchestration patterns (a menu, not a decision)

These can be mixed per step-type:

1. **Best-of-N + rerank.** N workers generate; rank by objective signal, break
   ties with a judge. Simple, robust, embarrassingly parallel. Good default.
2. **Generator ↔ Critic.** Model A writes, model B critiques against the spec/
   tests, A revises. Iterative; mirrors rubber-ducking. Good for reasoning-heavy
   or design steps where objective signals are weak.
3. **Debate / adversarial.** Two workers argue for their approach; a judge (or the
   test suite) decides. Useful for architecture/design forks.
4. **Synthesis / merge.** A synthesizer model is given the top candidates and asked
   to produce a merged best-of-both. Powerful but risky — merges can introduce
   incoherence; **only accept a merge if it re-passes the objective gate.**
5. **Role rotation.** Rotate planner/worker/critic across models so no single model
   dominates and blind spots differ each step.

## 5. Selection & verification — the signal ladder

Prefer **objective ground truth**; fall back to models only to break ties. Rough
priority order (strongest first):

1. **Does it build / compile / type-check?** (hard gate — discard failures)
2. **Do the tests pass?** existing tests + tests *generated for this task*.
   (Careful: a worker writing its own passing test is a conflict of interest —
   have a *different* model write/mutate the tests, or require property/edge tests.)
3. **Lint / static analysis / diff sanity** (size, touched files in scope).
4. **Self-consistency:** if K independent workers converge on ~the same answer,
   confidence is high (cheap signal, no judge needed).
5. **LLM-as-judge** — last resort, with guardrails: pairwise not absolute scoring,
   **swap positions to cancel position bias**, use a structured rubric, and use a
   model that is *not* one of the candidates' authors where possible.

Key principle: **let compilers and tests be the judge whenever possible** — they
don't hallucinate. Reuse our existing `evals/` checkers (`check.py`, the jsdom
playtest harness) as ready-made objective verifiers.

## 6. Model diversity strategy

Diversity is the value source, so deliberately vary:

- **Family/base:** Qwen (coding, chat, big) vs Gemma (fast/gemma-26b) — genuinely
  different lineages already on the box.
- **Size/quant:** Q6_K coding vs BF16 `big` vs Q4 Gemma — different failure modes.
- **Temperature / sampling:** even one model at 2–3 temperatures gives cheap
  diversity for best-of-N when a second model isn't resident.
- **Prompt/persona:** "make it minimal" vs "make it robust" workers surface
  different tradeoffs to arbitrate between.

## 7. Hardware reality on *this* box (the hard constraint)

This is where a generic "run N models" design meets our 2×V100 + 1×Titan-X, and
**llama-swap is single-slot** — swapping models per step costs a full model load.
Options:

- **(A) Use the already-co-resident trio as the ensemble** — *preferred*.
  In `daily` mode we simultaneously host three different-family models:
  `coding` (Qwen3.6-27B, idx1), `chat` (Qwen3.6-35B-A3B MoE, idx2), `fast`
  (Gemma, idx0). That's a natural, **zero-swap 3-model ensemble** reachable via
  LiteLLM `:4000`. Multipass should target these slots so a "pass" = three
  concurrent calls to already-warm models, not three sequential model loads.
- **(B) Temperature/persona ensemble within one slot.** When only one big slot is
  free (e.g. `heavy-coding` gives `coding` full ctx + `chat`/`fast` sub-agent
  pools), get diversity from multi-sample best-of-N on the resident model plus a
  smaller cross-checker. `--parallel` pools make concurrent samples cheap.
- **(C) A dedicated `multipass` serving mode.** Like the existing `daily`/
  `heavy-coding`/`agentic` overlays (`scripts/llama-swap-mode.py`), define a mode
  that pins the exact ensemble + parallelism multipass wants (e.g. coding P=2 on
  idx1, chat P=2 on idx2, fast P=2 on idx0) and warms them.
- **Avoid** the naïve "swap between 5 models every step" design — model-load
  latency would dominate wall time and kill the economics.

Implication: **the ensemble roster is a function of the active serving mode.**
Multipass should *query* what's resident (the status service already knows) and
adapt — not assume a fixed set.

## 8. Candidate isolation — git worktrees

To let multiple workers edit the *same* repo without stepping on each other, give
each candidate its own **git worktree** off the current step's base commit:

```
git worktree add .multipass/cand-A <base>
git worktree add .multipass/cand-B <base>
# each worker edits its worktree; run build/tests there; compare; then
# merge/cherry-pick the winner into the real branch, prune the worktrees.
```

Clean, disposable, parallel-safe, and it makes "apply the winner" a normal git
operation. (Everything under `.multipass/` stays gitignored.)

## 9. State, checkpointing, resumability

Long plan/build runs must survive interruption (and our quiet-hours model
juggling). Borrow the pattern this very session uses:

- **Task DAG** with statuses (pending/in-progress/done/blocked) + deps — a durable
  per-run SQLite store fits perfectly (mirrors the `todos`/`todo_deps` pattern).
- **Per-node checkpoint**: base commit, candidates, chosen winner, verifier
  output, judge rationale — for auditability and resume.
- **A git commit per verified node** so history *is* the progress log and any node
  is revertable.

## 10. Adaptive / economic controller (the "when to spend" brain)

Running every step at N=3 + judging is wasteful on trivial steps. Make the budget
**adaptive**:

- **Confidence-gated ensembling:** start solo; if the step is high-risk (touches
  many files, security-sensitive, architectural) *or* the first pass fails its
  gate, escalate to N workers + arbitration.
- **Disagreement-triggered escalation:** cheap 2-model check first; only pull in a
  third model / a bigger model / more passes when the two disagree.
- **Diminishing-returns cutoff:** stop adding passes when candidates converge or
  the marginal verifier score gain flattens.
- **Budget caps:** per-run token/time ceilings; degrade gracefully to solo.

This is what keeps multipass from being "5× slower for 5% better everywhere."

## 11. Failure modes & mitigations

| Risk | Mitigation |
|---|---|
| **Correlated errors** (same-base models agree on a wrong answer) | Enforce family/quant/temperature diversity; weight objective signals over agreement. |
| **Unreliable LLM judge** (verbosity/position/self bias) | Prefer objective gates; pairwise + position-swap; rubric; non-author judge. |
| **Worker grades its own homework** (writes a test that passes its bug) | A *different* model writes/mutates tests; require edge/property cases. |
| **Cost/latency explosion** | Adaptive controller (§10); target co-resident slots (§7); parallelize. |
| **Merge incoherence** | Re-run the objective gate on any merged candidate; prefer select-over-merge. |
| **Context growth over long runs** | Per-node summaries + checkpoints; don't carry raw transcripts. |
| **Swap thrash on single-slot GPUs** | Pin an ensemble mode; adapt roster to what's resident. |
| **Non-determinism muddies A/B** | Fixed seeds where possible; report distributions, not single runs. |

## 12. Implementation options (build-on-what)

- **(i) Thin custom orchestrator (Python) over LiteLLM `:4000`.** Max control,
  minimal deps; we own the loop, the worktrees, the SQLite state. Best for
  experimenting with arbitration strategies. *Leading candidate for a prototype.*
- **(ii) Extend the Copilot CLI (BYOK).** It already talks to our models, has
  sub-agents + a rubber-duck reviewer, and we already run agent-mediated evals
  through it (`scripts/eval-run-copilot.py`). Could express multipass as an
  orchestration layer that *drives* Copilot workers — less loop code, but less
  control over the per-step ensemble.
- **(iii) Adopt an existing agent (Aider / OpenHands / etc.) as the "worker"** and
  wrap N of them with our arbitration layer. Fastest to a working build loop;
  we'd be integrating, not inventing.
- **(iv) Expose it as an MCP tool / serving mode** so any client (Open WebUI,
  Copilot) can invoke a "multipass build" — consistent with our MCP-heavy stack.

A likely path: prototype with **(i)** to nail the arbitration/verification core,
reuse **(ii)**'s eval harness for measurement, and later surface via **(iv)**.

## 13. How we prove the thesis (measurement plan)

We already have the machinery to A/B this honestly:

- **Reuse the `evals/` framework** (objective `check.py` + the jsdom playtest
  harness) as the benchmark suite. Run **single-model** vs **multipass** on the
  same tasks (`local-dungeon-web`, `landing-page-pro`, `logic-puzzle`, …).
- **Primary metric:** one-shot success rate (fraction of tasks that end green
  *without* human intervention).
- **Report the cost curve too:** tokens + wall-time + GPU-seconds per solved task,
  so we can see the quality/cost trade explicitly and find the knee.
- **Stretch goal / north star:** local multipass ≥ a named frontier model's solo
  one-shot rate on the same suite, at acceptable wall-time.
- The Copilot-BYOK runner already does model-vs-model A/B — extend it with a
  `--multipass` target.

## 14. Phased roadmap (proposal)

1. **P0 — spike:** single task, 2 co-resident models (coding + chat), best-of-N
   with an *objective-only* gate (compile + existing test). No judge yet. Prove
   the worktree + arbitration plumbing end-to-end on one `evals/` task.
2. **P1 — planner + DAG + checkpoints:** decompose a real prompt into a task DAG;
   run the loop node-by-node with per-node commits and resumability.
3. **P2 — arbitration depth:** add critic/merge patterns + LLM-judge tie-break
   (with position-swap); add generated/mutated tests written by a *different* model.
4. **P3 — adaptive controller:** confidence-gating + disagreement escalation +
   budget caps; add a `multipass` serving mode.
5. **P4 — measure & iterate:** full eval-suite A/B vs solo and vs a frontier
   baseline; publish the cost/quality curves; write the ADR(s).

## 15. Open questions (need @bradrlaw's steer)

- **Scope of first target:** general software tasks, or narrow to the kind of
  web/coding tasks our evals already cover (so we can measure immediately)?
- **Ensemble size / composition:** hard 2-model (coding+chat) to start, or go
  straight to the 3-way with `fast`? Include a temperature-diversity variant?
- **Build on Copilot CLI vs. a standalone orchestrator** for the prototype?
- **How much to lean on generated tests** as the objective signal (powerful but
  needs a trustworthy test author)?
- **Interface:** CLI tool, MCP tool, an Open WebUI "build" button, or all three
  eventually?
- **Human-in-the-loop:** fully autonomous long runs, or checkpoint gates where a
  human can approve/redirect at plan boundaries?
- **Naming/roles:** Planner / Worker / Critic / Synthesizer / Verifier /
  Orchestrator — agree on the vocabulary before we start coding.

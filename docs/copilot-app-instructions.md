# GitHub Copilot desktop app — local model routing

How to drive the three local BYOK models (`coding`, `chat`, `fast`) from the GitHub Copilot
desktop app so subagents and parallel work land on **different GPUs**. Verified live
2026-07-18 against this AI server. See also `docs/server-setup.md` →
"Subagent model routing (GPU-tiered)" for the full findings and the SDK-host alternative.

## The three models / GPUs

| BYOK id | Model | GPU | Character |
| --- | --- | --- | --- |
| `coding` | Qwen3.6-27B (dense, Q6_K) | V100 idx1 | Best overall output — the driver + heavy reasoning |
| `chat` | Qwen3.6-35B-A3B (MoE) | V100 idx2 | Almost as good, higher throughput — parallel review/second-opinion |
| `fast` | Gemma-4-12B | P100 idx0 | Cheapest card, always warm — noisy explore + command-running |

Register all three as BYOK models in the app pointed at the LiteLLM gateway
(`http://<host>:4000/v1`). **Use the exact lowercase ids** — LiteLLM is case-sensitive, so
`Chat`/`Coding` return `400 Invalid model name` (call `/v1/models` for the canonical list).

## Two ways to get parallelism

### 1. One session, multiple subagents (in-session fan-out)

Put the block below into the app's **global custom instructions** (applies to all sessions).
The driver honors explicit model ids when it spawns subagents, routing each to the mapped
GPU. Verified: a "dispatch parallel agents" / `/review`-style prompt ran `coding` (idx1) and
`chat` (idx2) concurrently.

> **Caveat:** this only fires when the prompt clearly calls for delegation (e.g. `/review`,
> "dispatch parallel subagents", "compare two approaches"). Open-ended single questions
> (e.g. "give me an architecture overview") stay **inline on the driver** — the auto
> explore/search subagent is gated by a server-only account flag
> (`copilot_swe_agent_cli_search_subagent` = `off`) and won't spawn on its own.

### 2. Multiple sessions, one model each (cross-session parallelism)

The app can run **several sessions at once on the same repo**, each pinned to a different
model via its session dropdown. This is the simplest, most deterministic parallelism:

- Session A → `coding` (V100 idx1): the main implementation / hard reasoning task.
- Session B → `chat` (V100 idx2): a parallel review, refactor, or second workstream.
- Session C → `fast` (P100 idx0): quick lookups, running tests/builds, scratch questions.

Because each model lives on its own GPU, the three sessions run with **no contention and no
eviction**. Keep to one heavy session per V100; `fast` on the P100 is effectively free.

## Paste-ready global instructions

```
## Local model → subagent routing (BYOK: coding, chat, fast)

When delegating to a subagent, ALWAYS pass an explicit model id, matching one of the
locally-registered BYOK models EXACTLY (all lowercase — the gateway is case-sensitive;
"Chat" or "Coding" will fail with 400 Invalid model name). Use this mapping:

- explore / search / codebase-overview subagents → model: fast
- task subagents that run tests, builds, lints, or shell commands → model: fast
- code-review subagents → model: chat
- rubber-duck / second-opinion subagents → model: chat
- research subagents → model: chat
- general-purpose subagents (complex multi-step reasoning + editing) → model: coding
- security-review subagents → model: coding

For parallel reviews, spawn one reviewer on `coding` and one on `chat` so they run on
separate GPUs concurrently. Never route two heavy subagents to the same model at once.

Do NOT use the plan-build MCP tools during a review or multi-subagent session.
```

## Power draw (dual-GPU parallel work)

Measured live 2026-07-18 with two models running concurrently (`coding` on V100 idx1 +
`chat` on V100 idx2, both generating): **whole-server draw ~400–485 W total, ~4–4.5 A max**
at the wall. Keep this in mind for UPS sizing/limits — running three heavy sessions (both
V100s + P100) will push it higher. (The earlier dual-card vLLM benchmark that overloaded the
UPS is why the server was moved off it.)

## Gotchas (learned live 2026-07-18)

- **Exact lowercase model ids** — LiteLLM is case-sensitive; `Chat` → `400 Invalid model name`.
- **Do NOT expose the `plan-build` MCP to a review/multi-subagent session.** Its tools are
  serial (blocking calls) and the heavy ones swap `big`/`coder-next` onto **both V100s**,
  evicting the `coding`/`chat` models the subagents run on (the P100-only guard is bypassed
  by the HTTP service's `PLAN_BUILD_CALLER_GPU=p100` fallback). Symptom: a reviewer stalls
  at ~2k generated tokens as its model is evicted mid-flight. Turn plan-build off for these
  sessions (or `COPILOT_PLAN_BUILD_MCP=0` for the CLI launcher).
- **One heavy session/subagent per V100.** Don't point two heavy tasks at the same model —
  they serialize on the single llama-server slot. Spread across `coding`/`chat`/`fast`.

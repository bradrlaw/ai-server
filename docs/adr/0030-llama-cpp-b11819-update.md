# ADR-0030: Update llama.cpp from b9850 to b11819

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** @bradrlaw (+ Copilot CLI)
- **Related:** [ADR-0014](0014-p100-fast-fp16-carveout.md) (the P100 carveout patch is still
  re-applied by the build script), [ADR-0029](0029-qwen3.8-27b-migration.md) (the Qwen3.8 slots that
  gain the most), [ADR-0007](0007-litellm-openai-api-gateway.md) (LiteLLM tool-schema hook)

## Context
Production llama.cpp was **b9850** (`4f31eedb0`, 2026-06-30) for almost four months. Upstream was
1,969 commits ahead at **b11819** (`0693dbfb9`, 2026-10-22). Two independent research threads said
the newer code runs faster on V100s: the Qwen3.8-Flash-Next plan (§15) and the Hermes
`v100-perf-scout` cards. They cited CUDA graphs on Volta (#25749) and sm_70 K-quant tuning
(#28912). Nobody had measured either with our slot flags, and one cited "+27%" turned out to apply
only to a Q4_K_XL model with a DFlash2 drafter.

A blind update was too risky. Every agent client (OpenClaw, Hermes, pi, Open WebUI) depends on
tool-call parsing and the chat template, and both changed a lot across those commits. So the
update went through an isolated A/B first.

## Decision
**Promote b11819 as a drop-in.** No change to `config/llama-swap.base.yaml`, the mode overlays or
LiteLLM. The process, so it can be repeated:

1. **Build a candidate beside production.** `src/llama.cpp-b11819` (archs `52;70`, NCCL linked,
   niced). Production `build/` and the running services were never touched.
2. **A/B with the real slot flags** in one daytime window (2026-10-22 14:26–16:01Z, daily trio
   down about 95 minutes), runs ordered A, B, B, A. Full method and tables:
   [benchmarking.md](../benchmarking.md#llamacpp-update-b9850-to-b11819--ab-on-the-v100s-2026-10-22).
3. **Gates, all passed before promotion:**
   - no slot loses more than 2% decode or 3% prefill;
   - VRAM at the production context grows by at most 300 MiB (measured: +36 to +104 MiB);
   - KLD / PPL parity on `coding`, `chat` and `big` (PPL unchanged on all three; `chat`'s KLD equals
     the same-build noise floor);
   - the prompt-cache test shows no defect.
4. **Keep a golden copy.** `src/llama.cpp/build.b9850-golden/` is a hand-made copy of the old
   build that `scripts/build-llama.sh` never prunes.
5. **Promote with the normal script.** Fast-forward the production clone to b11819, then
   `scripts/build-llama.sh`: it re-applied the carveout patch, built into `build.new/` with archs
   `52;60;61;70`, checked the SASS, and swapped it live at 16:53Z. llama-swap was unloaded and the
   daily trio re-warmed at 16:55Z.
6. **Check it live** before calling it done (results below).

`scripts/build-llama.sh` needed one fix: from upstream #26839 (2026-08-12) `--version` prints
`0.7.0-dev (build 11819, commit 0693dbfb9)`, which its old-commit parser did not recognise, so the
next rollback folder would have been `build.prev-unknown-*`. It now reads both formats (harness
scenarios S17/S18).

### Measured gains (A = b9850, B = b11819)

| slot | decode (no MTP) | decode with MTP | prefill | VRAM at production ctx | quality |
|---|---|---|---|---|---|
| `coding` Q6_K | 22.5 → 26.4 t/s (**+17%**) | code **+23–25%**, summary +11–26% | +4–7% | +104 MiB | KLD 1.6e-4, same top-1 99.87% |
| `chat` UD-Q6_K MoE | +3–4% | +1.4–3.8% | +5–13% | +36 MiB | ln PPL ratio −0.00006 |
| `big` Q8_K_XL | +1.3% | +1–9% | +7–10% | +278 MiB (ctx 40960, MTP) | KLD 4.2e-4 |
| `small` Titan X | 0% | n/a | +2% | n/a | not run |

**The cause is not CUDA graphs.** The research credited #25749, but running B with
`GGML_CUDA_DISABLE_GRAPHS=1` changes nothing (`coding` tg128 26.4 t/s either way). The gain follows
the quant type (Q6_K +17%, the MoE's UD-Q6_K +3%, Q8_0-based `big` +1.3%, the Titan X 0%), which
points at the K-quant MMVQ kernels: #30517 (low-bit types), #29753 (sm_70 uses the Turing `nwarps`
table), #28912 (MMVQ→MMQ crossover), #29184 (shared-expert fusion). I did not bisect them.

### Live checks after the swap

Run on the production build after the 16:55Z re-warm, in `daily` mode with the real slot flags.
Scripts and raw output:
[`docs/data/llamacpp-b11819-ab-20261022/live/`](../data/llamacpp-b11819-ab-20261022/live/); the
detailed tables are in
[benchmarking.md](../benchmarking.md#live-checks-after-the-swap-2026-10-22).

| check | result |
|---|---|
| Build and swap | `scripts/build-llama.sh` took 13 min (niced). `--version` is `0.7.0-dev (build 11819, commit 0693dbfb9)`, the SASS covers sm_52/60/61/70 and the carveout patch is applied. Live at 16:53:52Z. |
| Re-warm | `/unload`, then `llama-swap-mode.py set daily`: `chat`, `coding`, `small` ready in 37 s. VRAM idx0 / idx1 / idx2 = 8,598 / 30,774 / 31,524 MiB, the same as on b9850. |
| Smoke tests | One 300-token story per slot: `coding` 53.6 t/s (MTP), `chat` 129 t/s, `small` 20 t/s, matching `llama-bench`. The same question through LiteLLM `:4000`, streaming and not, is answered correctly on all three. |
| Tool-call suite | 15 cases × 2 repetitions per slot, A → B: `coding` 28/30 → 30/30, `chat` 30/30 → 29/30, `small` 28/30 → 27/30. No HTTP errors, malformed JSON or markup leaks on either build. The misses are the models' own choices, not parse losses (next two rows). |
| Parallel calls | Raw model output against the parsed `tool_calls` for the **same** sample (llama-server's `verbose` field): the counts match in 48 of 48 samples (`chat` 24, `small` 24). A single call where two were asked for is the model writing one call. |
| `tool_choice` | llama.cpp ignores the named-function form (it reads the field as a string and falls back to `auto`), so the suite's `forced_choice` case only measures the model's willingness to call the tool: `coding` did so in 21 of 40 tries on b11819, `big` 8/10, `chat` and `small` 10/10. That is why `coding` went 0/2 → 2/2 between A and B: noise, **not a fix**. `"tool_choice": "required"` is honoured: `coding` 20/20, `chat` 38/40, `small` 20/20 (not measured on b9850). |
| OpenClaw `cron` request | b9850 rejected it with HTTP 400 (grammar repetition limit); b11819 returns 200 straight from llama-swap on `chat`, `coding` and `small`, with a valid `{"action":"list","includeDisabled":true}` call. |
| 30-minute soak | Three workers (one per slot), 205 requests (code, ~7.6k-token summary, Q&A, and two long needles: `coding` at 107,909 tokens, 252 s, prefill 433 t/s, and `chat` at 33,281 tokens, 40 s, prefill 932 t/s, both found). 0 failed requests, no `CUDA error` / `GGML_ASSERT` / out-of-memory / abort lines in the journal, decode flat across the thirds of the run (`coding` code 49.5 / 48.6 / 49.4 t/s), VRAM constant (idx0 8,835, idx1 31,276, idx2 31,882–31,886 MiB), peak temperatures idx0 85 °C (mean 83 °C in the last third: the Titan X's 83 °C boost target), idx1 70 °C, idx2 57 °C. |
| `big` | Loads in 13 s; 23,884 / 28,472 MiB at the production 262k context, unchanged after a 65,870-token prompt (needle found, prefill 637 t/s). Code decode 41.8 t/s. Tool-call suite 29/30. |
| `coder-next` | Cold load from the HDD 315 s; 28,718 / 27,332 MiB. Code decode **80.0 t/s** (the 2026-08-24 sweep gave about 74 t/s at P=1 with a different prompt, so +8% is indicative only: no A-side run). 64,759-token needle found (prefill 340 t/s). Tool-call suite 29/30. |
| GSM8K (n=100, 5-shot, greedy) | `coding` slot at its production `reasoning_effort=medium`: flexible **0.99**, strict **0.99** (one miss, the classic "year 13" off-by-one). The 2026-08-15 reference was flexible 0.99 / strict 0.92 at xhigh on a standalone server, so accuracy is at parity; the strict difference is not attributed to the build because the settings differ. 14 min 34 s. |

Of the 110 `chat` soak requests, 61 ended with `finish=length` and empty content because the
thinking phase used the whole 1,200 to 3,000-token budget. That is the soak's budget, not a defect:
the tool-call suite's `chat` completions are the same length on both builds (median 203 tokens on
A, 216 on B).

## Consequences
- Positive:
  - `coding` (the interactive slot) decodes about 17% faster raw and 20–25% faster on real
    thinking-on traffic, at the same context and about 100 MiB more VRAM.
  - Prefill is 2–13% faster on every slot, and cached-prefix reuse is better on follow-up turns
    (turn 2 of the 20k-token test reprocesses 191 tokens instead of 2,258).
  - The OpenClaw `cron` request that b9850 rejected with HTTP 400 (grammar repetition limit) now
    returns 200 straight from llama-swap on all three slots. Tool-call parsing shows no regression
    (48 of 48 paired raw-vs-parsed samples match).
  - A repeatable, gated update process, and `build.b9850-golden/` as a permanent fallback.
- Negative / trade-offs:
  - Almost 2,000 upstream commits landed at once. The golden copy and the checks above bound
    that risk, but they cannot rule out a rare regression.
  - `big` gains little (+1.3%). It is bandwidth-bound already, so it is a prefill win only.
  - The production build has no NCCL (the test build had it). That only matters if tensor
    parallelism (`-sm tensor`) is tried later; rebuild with NCCL then.
  - The configure step now fetches prebuilt web-UI assets from the Hugging Face bucket
    `ggml-org/llama-ui` (`LLAMA_UI_HF_BUCKET`), so a build needs Hugging Face reachable. It is a
    build-time dependency only: the UI is embedded in `llama-server`.
  - `coder-next` is built from K-quants like `coding`, but it had no A-side measurement, so
    its gain is not quantified against b9850 (80 t/s on b11819 against about 74 t/s in the
    2026-08-24 sweep, measured with a different prompt).
- Follow-ups / things to watch:
  - **Watch for 48 hours:** llama-swap journal errors (`CUDA error`, `GGML_ASSERT`, checkpoint
    loops), VRAM trend on idx1/idx2, the Hermes `v100-perf-scout` run at 01:00 ET (uses `big`), and
    OpenClaw sessions. Roll back with
    `cd /srv/ai/src/llama.cpp && mv build build.bad && cp -a build.b9850-golden build`, then
    `curl 127.0.0.1:9090/unload && scripts/llama-swap-mode.py set daily`.
  - `coding --spec-draft-n-max 4`: +8–13% on echo-heavy code, about +2% on real multi-turn traffic,
    +152 MiB. Needs a context-fit run at 163840 first. n=5 is not worth +300 MiB.
  - `--spec-draft-sampling probabilistic` helps `chat` (+2–8%) but not `coding`; skipped.
  - Untested ideas that need the owner at the console (tensor-parallel crashes can poison the
    UVM and need `sudo scripts/gpu-uvm-reset.sh`): `-sm tensor` on `big`, the DFlash2 drafter on
    `coding` (downloaded to `models/qwen3.8-27b-dflash2/`), q4_0 KV.
  - The LiteLLM schema hook (`docker/litellm/custom_hooks.py`, ADR-0007 addendum) is now redundant
    for llama.cpp ≥ b11819 on the one request we know about. Keep it until the cutoffs are
    re-measured on b11819.

## Alternatives considered
- **Stay on b9850.** Leaves +17% on the interactive slot unclaimed, and the gap widens with every
  month. The A/B made the update cheap enough to do.
- **Pull straight into production** (the old `git pull` + rebuild path). No before/after numbers, no
  window to test the load, and only the previous build as a rollback. Rejected in favour of the
  separate-clone process above.
- **Cherry-pick the MMVQ kernel PRs onto b9850.** A private fork to maintain for a few
  percent more than upstream gives us anyway; rejected.
- **Wait for a "release" tag.** llama.cpp builds every commit (`bNNNN`); there is no slower, more
  stable channel to wait for.

#!/usr/bin/env bash
# Launch GitHub Copilot CLI against the local LiteLLM gateway (BYOK).
# Docs: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models
#
# Reads LITELLM_MASTER_KEY from /srv/ai/docker/.env (gitignored) so no secret is
# hardcoded. Override the model with:  COPILOT_MODEL=chat copilot-byok.sh
# Per-model prompt/output token budgets are set automatically (see the case block below);
# override with COPILOT_PROVIDER_MAX_PROMPT_TOKENS / COPILOT_PROVIDER_MAX_OUTPUT_TOKENS.
#
# Usage (on the server):        /srv/ai/scripts/copilot-byok.sh
# Usage (remote via Tailscale): COPILOT_PROVIDER_BASE_URL=http://<tailscale-ip>:4000/v1 \
#                                 COPILOT_PROVIDER_API_KEY=sk-... copilot-byok.sh
set -euo pipefail

ENV_FILE=/srv/ai/docker/.env
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"

export COPILOT_PROVIDER_BASE_URL="${COPILOT_PROVIDER_BASE_URL:-http://127.0.0.1:4000/v1}"
export COPILOT_PROVIDER_TYPE="${COPILOT_PROVIDER_TYPE:-openai}"
export COPILOT_PROVIDER_API_KEY="${COPILOT_PROVIDER_API_KEY:-${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY not set (docker/.env)}}"
export COPILOT_MODEL="${COPILOT_MODEL:-coding}"

# Per-model token budgets. In llama.cpp the --ctx-size KV cache is SHARED between prompt
# and generation, so MAX_PROMPT + MAX_OUTPUT must stay under the model's ctx-size (with
# ~15-20% headroom for tokenizer drift + compute buffers). Reasoning models get a larger
# output budget because the hidden thinking phase spends output tokens. Values already set
# in the environment win, so you can override per invocation.
#   ctx-size / reasoning (see config/llama-swap.yaml):
#     coding     184320  reasoning  (Q6_K + MTP self-spec decode; 200k OOMs with MTP)
#     chat        98304  reasoning  (UD-Q6_K + MTP self-spec decode; 128k OOMs with MTP)
#     big        262144  reasoning
#     coder-next 262144 total / 131072 per slot  (--parallel 2, NON-thinking, agentic, ~77 t/s)
#     fast       32768   NON-thinking (Gemma-4-26B-A4B MoE; fast-12b fallback=131072)
case "$COPILOT_MODEL" in
  coding)     def_prompt=131072; def_output=32768 ;;   # 163840 <= 184320 (~20k spare)
  chat)       def_prompt=57344;  def_output=24576 ;;   # 81920 <= 98304 (~16k spare); MTP capped ctx to 96k
  big)        def_prompt=163840; def_output=32768 ;;
  # coder-next runs --parallel 2, so each slot is 131072, NOT the full 262144.
  # Keep prompt + output within one slot: 98304 + 32768 = 131072.
  coder-next) def_prompt=98304;  def_output=32768 ;;
  fast)       def_prompt=24576;  def_output=8192  ;;   # MoE, ctx 32768 (24576+8192)
  fast-12b)   def_prompt=98304;  def_output=8192  ;;   # dense 12B fallback, ctx 131072
  *)          def_prompt=32768;  def_output=8192  ;;  # conservative fallback for unlisted models
esac
export COPILOT_PROVIDER_MAX_PROMPT_TOKENS="${COPILOT_PROVIDER_MAX_PROMPT_TOKENS:-$def_prompt}"
export COPILOT_PROVIDER_MAX_OUTPUT_TOKENS="${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS:-$def_output}"

# --- Subagent model routing (GPU-tiered) -----------------------------------
# Run the token-heavy explore/search subagent on a DIFFERENT local model than the
# driver so it executes on a SEPARATE GPU in parallel (no contention/eviction):
#     driver (COPILOT_MODEL, default 'coding') -> V100 idx1
#     explore/search subagent  -> 'fast' (Gemma-4-26B-A4B MoE) on the P100 (idx0), always warm
# The P100 is on a different card from every V100 driver, so the driver keeps
# reasoning while explores run in parallel with zero cold-start (fast keeper thread).
# Override with SEARCH_SUBAGENT_MODEL=<id>; set it EMPTY to inherit the driver.
#
# CAVEATS (verified 2026-07-18; see docs/server-setup.md "Subagent model routing"):
#  * SEARCH_SUBAGENT_MODEL is INERT via this env-var launcher: the search subagent
#    is gated by the account feature flag copilot_swe_agent_cli_search_subagent,
#    whose availability is "off" (server-only) — NOT reachable by env,
#    COPILOT_CLI_ENABLED_FEATURE_FLAGS, or /experimental. Proven: a delegated explore
#    kept `fast` at 0 tokens. Kept here so it auto-activates if GitHub enables the flag.
#  * Per-subagent local models (e.g. task->chat on idx2, explorer->fast on P100) are
#    NOT possible through single-provider env-var BYOK (the /agents picker only lists
#    the one configured model). They ARE possible two ways:
#      (a) @github/copilot-sdk host: CopilotClient({onListModels}) + createSession(
#          {provider:LiteLLM, customAgents:[{model:"chat"}]}) — PROVEN live (explorer
#          ran 81k tokens on chat/idx2 while driver stayed on coding/idx1).
#      (b) GitHub Copilot desktop app: configure coding/chat/fast as separate BYOK
#          models (exact lowercase ids — LiteLLM is case-sensitive; `Chat` 400s), then
#          ask for a multi-model review ("...using the coding model and the chat model").
#          PROVEN live 2026-07-18: parallel coding(idx1)+chat(idx2) reviewers. BUT do NOT
#          expose the plan-build MCP to such a session (COPILOT_PLAN_BUILD_MCP=0) — it's
#          serial and swaps big/coder-next onto both V100s, evicting the reviewers' models.
export SEARCH_SUBAGENT_MODEL="${SEARCH_SUBAGENT_MODEL-fast}"

# Register the plan-build MCP server (planner->coder pipeline) with the Copilot CLI.
# It runs as a native HTTP service on the AI server (scripts/plan-build-mcp.service,
# 0.0.0.0:9100), so the client needs no Python/uv — just `copilot mcp add`. The URL
# is derived from COPILOT_PROVIDER_BASE_URL (same host, port 9100, path /mcp);
# override with PLAN_BUILD_MCP_URL, or opt out with COPILOT_PLAN_BUILD_MCP=0.
if [[ "${COPILOT_PLAN_BUILD_MCP:-1}" != "0" ]] && command -v copilot >/dev/null 2>&1; then
  if [[ -n "${PLAN_BUILD_MCP_URL:-}" ]]; then
    pb_url="$PLAN_BUILD_MCP_URL"
  else
    pb_proto="${COPILOT_PROVIDER_BASE_URL%%://*}"
    pb_rest="${COPILOT_PROVIDER_BASE_URL#*://}"
    pb_host="${pb_rest%%/*}"; pb_host="${pb_host%%:*}"
    pb_url="${pb_proto}://${pb_host}:${PLAN_BUILD_MCP_PORT:-9100}/mcp"
  fi
  # Idempotent: only add if not already registered (never fail the launch on error).
  if ! copilot mcp list 2>/dev/null | grep -q '\bplan-build\b'; then
    if copilot mcp add --transport http plan-build "$pb_url" >/dev/null 2>&1; then
      echo "Registered plan-build MCP -> ${pb_url}" >&2
    else
      echo "NOTE: could not register plan-build MCP (${pb_url}); add it manually:" >&2
      echo "      copilot mcp add --transport http plan-build ${pb_url}" >&2
    fi
  fi
fi

echo "Copilot CLI -> ${COPILOT_PROVIDER_BASE_URL} (model: ${COPILOT_MODEL}, prompt<=${COPILOT_PROVIDER_MAX_PROMPT_TOKENS}, output<=${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS}${SEARCH_SUBAGENT_MODEL:+, search-subagent: ${SEARCH_SUBAGENT_MODEL}})" >&2
exec copilot "$@"

#!/usr/bin/env bash
# Launch GitHub Copilot CLI against the local LiteLLM gateway (BYOK).
# Docs: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models
#       docs/server-setup.md "GitHub Copilot CLI via BYOK"
#
# Default (providers.json) mode: writes every local model to
# $COPILOT_HOME/aiserver-providers.json (COPILOT_PROVIDERS_CONFIG) and starts
# `copilot --model aiserver/<model>`. /model then lists and switches all local models,
# and CLI 1.0.114+ does not swap them for a managed default (the legacy COPILOT_PROVIDER_*
# env vars get "An administrator-managed setting replaced model ...").
#
# Reasoning effort: the /model picker can't show levels for BYOK models, so each reasoning
# model also gets one entry per effort (flash-low, coding-high, ... -> LiteLLM `model@effort`),
# and `--reasoning-effort <level>` works too (the provider sends X-AIServer-Reasoning:
# passthrough; docker/litellm/custom_hooks.py). The plain entry uses the server default.
#
# Usage (on the server):   /srv/ai/scripts/copilot-byok.sh            (COPILOT_MODEL=flash ...)
# Usage (another machine): AISERVER_BASE_URL=http://<tailscale-ip>:4000/v1 copilot-byok.sh
#   API key, never written to disk: AISERVER_API_KEY_COMMAND, else docker/.env on the server,
#   else the macOS keychain item `aiserver-litellm`
#   (create: security add-generic-password -s aiserver-litellm -a "$USER" -w),
#   else Linux `secret-tool lookup service aiserver-litellm`.
# Legacy single-model env-var mode: COPILOT_BYOK_LEGACY=1 copilot-byok.sh (used automatically
#   when the installed CLI predates providers.json, e.g. 1.0.75). COPILOT_BIN=<path> picks the CLI.
#
# Models: coding (default), chat, big, flash, coder-next, small, small-uncensored, gemma-31b,
# gemma-26b, chat-uncensored-q6. Flash-Next:  COPILOT_MODEL=flash copilot-byok.sh
#   (first request cold-loads ~90 s and evicts coding/chat/big; any later request for those evicts flash)
set -euo pipefail

ENV_FILE=/srv/ai/docker/.env
COPILOT_BIN="${COPILOT_BIN:-copilot}"
COPILOT_MODEL="${COPILOT_MODEL:-coding}"
BASE_URL="${AISERVER_BASE_URL:-${COPILOT_PROVIDER_BASE_URL:-http://127.0.0.1:4000/v1}}"

# Per-model token budgets. In llama.cpp the --ctx-size KV cache is SHARED between prompt
# and generation, so MAX_PROMPT + MAX_OUTPUT must stay under the model's ctx-size (with
# ~15-20% headroom for tokenizer drift + compute buffers). Reasoning models get a larger
# output budget because the hidden thinking phase spends output tokens. Values already set
# in the environment win, so you can override per invocation.
#   ctx-size / reasoning (see config/llama-swap.yaml):
#     coding     163840  reasoning  (Qwen3.8-27B Q6_K + MTP self-spec decode; reasoning_effort=medium)
#     chat        98304  reasoning  (UD-Q6_K + MTP self-spec decode; 128k OOMs with MTP)
#     big        262144  reasoning  (Qwen3.8-27B UD-Q8_K_XL + MTP, dual-V100, q8 KV)
#     flash      524288  reasoning  (Qwen3.8-Flash-Next 125B MoE on Strata, 512K, vision; ADR-0033)
#     coder-next 262144 total / 131072 per slot  (--parallel 2, NON-thinking, agentic, ~77 t/s)
#     small      32768   NON-thinking (Gemma-4-12B dense on the idx0 Titan X stopgap; small-uncensored same)
#     gemma-31b / gemma-26b / chat-uncensored-q6   131072  (comparison / uncensored chat)
budget_for() {
  case "$1" in
  coding)     def_prompt=114688; def_output=32768 ;;   # 147456 <= 163840 (~16k spare); 3.8 ctx dropped 184320->163840
  chat)       def_prompt=57344;  def_output=24576 ;;   # 81920 <= 98304 (~16k spare); MTP capped ctx to 96k
  big)        def_prompt=163840; def_output=32768 ;;
  # coder-next runs --parallel 2, so each slot is 131072, NOT the full 262144.
  # Keep prompt + output within one slot: 98304 + 32768 = 131072.
  # NOTE: coder-next weights live on the bulk cold tier (2026-08-18), so the FIRST
  # request after idle eviction (ttl 300) blocks ~5min on the 49.6GB HDD read.
  coder-next) def_prompt=98304;  def_output=32768 ;;
  # flash uses BOTH V100s and evicts coding/chat/big (a 1.5-2 min swap each way); small on
  # idx0 coexists. Prefill is ~900 tok/s at depth, so a full 512K prompt takes ~10 min to first
  # token: the prompt budget is capped well below ctx so Copilot compacts earlier. Raise
  # COPILOT_PROVIDER_MAX_PROMPT_TOKENS (up to ~450000) for deliberate long-context work.
  flash)      def_prompt=262144; def_output=32768 ;;
  gemma-31b|gemma-26b|chat-uncensored-q6) def_prompt=81920; def_output=16384 ;;   # ctx 131072, non-reasoning/light thinking
  small|small-uncensored) def_prompt=20480;  def_output=8192  ;;   # dense 12B on Titan X, ctx 32768 (20480+8192, ~4k spare)
  *)          def_prompt=32768;  def_output=8192  ;;  # conservative fallback for unlisted models
  esac
}

# --- Subagent model routing (GPU-tiered) -----------------------------------
# Run the token-heavy explore/search subagent on a DIFFERENT local model than the
# driver so it executes on a SEPARATE GPU in parallel (no contention/eviction):
#     driver (COPILOT_MODEL, default 'coding') -> V100 idx1   (with COPILOT_MODEL=flash the driver
#     occupies both V100s; 'small' on idx0 still coexists, so the search subagent setting is unchanged)
#     explore/search subagent  -> 'small' (Gemma-4-12B dense) on the Titan X (idx0), always warm
# The P100 is on a different card from every V100 driver, so the driver keeps
# reasoning while explores run in parallel with zero cold-start (small keeper thread).
# Override with SEARCH_SUBAGENT_MODEL=<id>; set it EMPTY to inherit the driver.
#
# CAVEATS (verified 2026-07-18; see docs/server-setup.md "Subagent model routing"):
#  * SEARCH_SUBAGENT_MODEL is INERT via this env-var launcher: the search subagent
#    is gated by the account feature flag copilot_swe_agent_cli_search_subagent,
#    whose availability is "off" (server-only) — NOT reachable by env,
#    COPILOT_CLI_ENABLED_FEATURE_FLAGS, or /experimental. Proven: a delegated explore
#    kept `small` at 0 tokens. Kept here so it auto-activates if GitHub enables the flag.
#  * Per-subagent local models (e.g. task->chat on idx2, explorer->small on idx0) are
#    NOT possible through single-provider env-var BYOK (the /agents picker only lists
#    the one configured model; providers.json mode lists them all, untested for subagents).
#    They ARE possible two ways:
#      (a) @github/copilot-sdk host: CopilotClient({onListModels}) + createSession(
#          {provider:LiteLLM, customAgents:[{model:"chat"}]}) — PROVEN live (explorer
#          ran 81k tokens on chat/idx2 while driver stayed on coding/idx1).
#      (b) GitHub Copilot desktop app: configure coding/chat/small as separate BYOK
#          models (exact lowercase ids — LiteLLM is case-sensitive; `Chat` 400s), then
#          ask for a multi-model review ("...using the coding model and the chat model").
#          PROVEN live 2026-07-18: parallel coding(idx1)+chat(idx2) reviewers. BUT do NOT
#          expose the plan-build MCP to such a session (COPILOT_PLAN_BUILD_MCP=0) — it's
#          serial and swaps big/coder-next onto both V100s, evicting the reviewers' models.
export SEARCH_SUBAGENT_MODEL="${SEARCH_SUBAGENT_MODEL-small}"

# Server-side default effort per model (config/llama-swap.base.yaml, config/strata/flash.json)
# and the extra per-effort /model entries. chat (Qwen3.6) only honours on/off.
default_effort_for() {
  case "$1" in
    flash) echo high ;;
    coding|big) echo medium ;;
    *) echo "" ;;
  esac
}
efforts_for() {
  case "$1" in
    flash|coding|big) echo "none low medium high xhigh" ;;
    chat|chat-uncensored-q6|small-uncensored) echo "none" ;;
    *) echo "" ;;
  esac
}
vision_for() {
  case "$1" in
    coding|chat|big|flash|small|small-uncensored) echo true ;;
    *) echo false ;;
  esac
}
MODELS="coding chat big flash coder-next small small-uncensored gemma-31b gemma-26b chat-uncensored-q6"

# COPILOT_MODEL may be a base model (flash) or an effort entry (flash-low); split it.
sel_model="$COPILOT_MODEL"; sel_base="$COPILOT_MODEL"; sel_effort=""
case " $MODELS " in
  *" $COPILOT_MODEL "*) ;;
  *) for m in $MODELS; do
       for e in $(efforts_for "$m"); do
         [[ "$COPILOT_MODEL" == "$m-$e" ]] && { sel_base=$m; sel_effort=$e; }
       done
     done ;;
esac

# NOTE: the plan-build MCP is serial and swaps big/coder-next onto both V100s, which evicts
# `flash`. With COPILOT_MODEL=flash, consider COPILOT_PLAN_BUILD_MCP=0.
# Register the plan-build MCP server (planner->coder pipeline) with the Copilot CLI.
# It runs as a native HTTP service on the AI server (scripts/plan-build-mcp.service,
# 0.0.0.0:9100), so the client needs no Python/uv — just `copilot mcp add`. The URL
# is derived from the base URL (same host, port 9100, path /mcp);
# override with PLAN_BUILD_MCP_URL, or opt out with COPILOT_PLAN_BUILD_MCP=0.
register_plan_build_mcp() {
  if [[ "${COPILOT_PLAN_BUILD_MCP:-1}" != "0" ]] && command -v "$COPILOT_BIN" >/dev/null 2>&1; then
    if [[ -n "${PLAN_BUILD_MCP_URL:-}" ]]; then
      pb_url="$PLAN_BUILD_MCP_URL"
    else
      pb_proto="${BASE_URL%%://*}"
      pb_rest="${BASE_URL#*://}"
      pb_host="${pb_rest%%/*}"; pb_host="${pb_host%%:*}"
      pb_url="${pb_proto}://${pb_host}:${PLAN_BUILD_MCP_PORT:-9100}/mcp"
    fi
    # Idempotent: only add if not already registered (never fail the launch on error).
    if ! "$COPILOT_BIN" mcp list 2>/dev/null | grep -q '\bplan-build\b'; then
      if "$COPILOT_BIN" mcp add --transport http plan-build "$pb_url" >/dev/null 2>&1; then
        echo "Registered plan-build MCP -> ${pb_url}" >&2
      else
        echo "NOTE: could not register plan-build MCP (${pb_url}); add it manually:" >&2
        echo "      copilot mcp add --transport http plan-build ${pb_url}" >&2
      fi
    fi
  fi
}

# --- Legacy env-var mode (single model; CLI 1.0.114+ may replace it, see header) ---------
cli_help=$({ "$COPILOT_BIN" help providers; "$COPILOT_BIN" help providers-local; } 2>/dev/null || true)
if [[ "${COPILOT_BYOK_LEGACY:-0}" != "1" && "$cli_help" != *providers.json* ]]; then
  echo "NOTE: $("$COPILOT_BIN" --version 2>/dev/null | head -1) has no providers.json support; using legacy env-var BYOK." >&2
  COPILOT_BYOK_LEGACY=1
fi
if [[ "${COPILOT_BYOK_LEGACY:-0}" == "1" ]]; then
  [[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
  budget_for "$sel_base"
  # Effort entries map to LiteLLM's model@effort form (docker/litellm/custom_hooks.py).
  [[ -n "$sel_effort" ]] && COPILOT_MODEL="$sel_base@$sel_effort"
  export COPILOT_PROVIDER_BASE_URL="$BASE_URL"
  export COPILOT_PROVIDER_TYPE="${COPILOT_PROVIDER_TYPE:-openai}"
  export COPILOT_PROVIDER_API_KEY="${COPILOT_PROVIDER_API_KEY:-${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY not set (docker/.env)}}"
  export COPILOT_MODEL
  export COPILOT_PROVIDER_MAX_PROMPT_TOKENS="${COPILOT_PROVIDER_MAX_PROMPT_TOKENS:-$def_prompt}"
  export COPILOT_PROVIDER_MAX_OUTPUT_TOKENS="${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS:-$def_output}"
  register_plan_build_mcp
  echo "Copilot CLI (legacy env BYOK) -> ${BASE_URL} (model: ${COPILOT_MODEL}, prompt<=${COPILOT_PROVIDER_MAX_PROMPT_TOKENS}, output<=${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS})" >&2
  exec "$COPILOT_BIN" "$@"
fi

# --- providers.json mode -----------------------------------------------------------------
# API key: a command the CLI runs per request, so the key is never written to disk.
if [[ -n "${AISERVER_API_KEY_COMMAND:-}" ]]; then
  key_cmd="$AISERVER_API_KEY_COMMAND"
elif [[ -n "${COPILOT_PROVIDER_API_KEY:-}" ]]; then
  export AISERVER_LITELLM_KEY="$COPILOT_PROVIDER_API_KEY"
  key_cmd="printenv AISERVER_LITELLM_KEY"
elif [[ -r "$ENV_FILE" ]]; then
  key_cmd="sed -n s/^LITELLM_MASTER_KEY=//p $ENV_FILE"
elif command -v security >/dev/null 2>&1; then
  if ! security find-generic-password -s aiserver-litellm >/dev/null 2>&1; then
    echo "No keychain item 'aiserver-litellm'. Create it (paste the LiteLLM key when asked):" >&2
    echo "  security add-generic-password -s aiserver-litellm -a \"\$USER\" -w" >&2
    exit 1
  fi
  key_cmd="security find-generic-password -s aiserver-litellm -w"
elif command -v secret-tool >/dev/null 2>&1; then
  key_cmd="secret-tool lookup service aiserver-litellm"
else
  echo "No LiteLLM key source: set AISERVER_API_KEY_COMMAND (see header)." >&2
  exit 1
fi

json_str() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

model_entry() {  # id wire name base
  local id=$1 wire=$2 name=$3 base=$4 p o
  budget_for "$base"; p=$def_prompt; o=$def_output
  if [[ "$base" == "$sel_base" ]]; then
    p="${COPILOT_PROVIDER_MAX_PROMPT_TOKENS:-$p}"; o="${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS:-$o}"
  fi
  printf '    {"id": %s, "wireModel": %s, "provider": "aiserver", "name": %s, "maxPromptTokens": %s, "maxOutputTokens": %s, "capabilities": {"supports": {"toolCalls": true, "vision": %s}}}' \
    "$(json_str "$id")" "$(json_str "$wire")" "$(json_str "$name")" "$p" "$o" "$(vision_for "$base")"
}

entries=()
for m in $MODELS; do
  d=$(default_effort_for "$m")
  entries+=("$(model_entry "$m" "$m" "$m${d:+ (effort $d)}" "$m")")
  for e in $(efforts_for "$m"); do
    [[ "$e" == "$d" ]] && continue
    entries+=("$(model_entry "$m-$e" "$m@$e" "$m · $e" "$m")")
  done
done
# Any other LiteLLM model id (e.g. v100-auto) gets its own entry so --model resolves.
if [[ -z "$sel_effort" ]] && [[ " $MODELS " != *" $sel_model "* ]]; then
  entries+=("$(model_entry "$sel_model" "$sel_model" "$sel_model" "$sel_model")")
fi

cfg_dir="${COPILOT_HOME:-$HOME/.copilot}"
mkdir -p "$cfg_dir"
export COPILOT_PROVIDERS_CONFIG="${COPILOT_PROVIDERS_CONFIG:-$cfg_dir/aiserver-providers.json}"
umask 077
{
  printf '{\n  "providers": [{\n    "name": "aiserver", "type": "openai", "baseUrl": %s,\n' "$(json_str "$BASE_URL")"
  printf '    "apiKeyCommand": %s,\n' "$(json_str "$key_cmd")"
  printf '    "headers": {"X-AIServer-Reasoning": "passthrough"}\n  }],\n  "models": [\n'
  sep=""
  for e in "${entries[@]}"; do printf '%s%s' "$sep" "$e"; sep=$',\n'; done
  printf '\n  ]\n}\n'
} > "$COPILOT_PROVIDERS_CONFIG"

# COPILOT_PROVIDER_* env vars take precedence over providers.json, so clear them.
unset COPILOT_PROVIDER_BASE_URL COPILOT_PROVIDER_TYPE COPILOT_PROVIDER_API_KEY \
      COPILOT_PROVIDER_BEARER_TOKEN COPILOT_PROVIDER_MAX_PROMPT_TOKENS \
      COPILOT_PROVIDER_MAX_OUTPUT_TOKENS COPILOT_PROVIDER_MODEL_ID COPILOT_PROVIDER_WIRE_MODEL \
      COPILOT_PROVIDER_WIRE_API COPILOT_MODEL

register_plan_build_mcp
echo "Copilot CLI -> ${BASE_URL} (model: aiserver/${sel_model}; ${#entries[@]} local models in ${COPILOT_PROVIDERS_CONFIG}; /model to switch)" >&2
exec "$COPILOT_BIN" --model "aiserver/$sel_model" "$@"

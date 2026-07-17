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
#     coding     204800  reasoning
#     chat       131072  reasoning
#     big        262144  reasoning
#     coder-next 262144  NON-thinking (agentic, cheap KV, ~77 t/s)
#     fast       131072  NON-thinking
case "$COPILOT_MODEL" in
  coding)     def_prompt=131072; def_output=32768 ;;
  chat)       def_prompt=81920;  def_output=24576 ;;
  big)        def_prompt=163840; def_output=32768 ;;
  coder-next) def_prompt=196608; def_output=32768 ;;
  fast)       def_prompt=98304;  def_output=8192  ;;
  *)          def_prompt=32768;  def_output=8192  ;;  # conservative fallback for unlisted models
esac
export COPILOT_PROVIDER_MAX_PROMPT_TOKENS="${COPILOT_PROVIDER_MAX_PROMPT_TOKENS:-$def_prompt}"
export COPILOT_PROVIDER_MAX_OUTPUT_TOKENS="${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS:-$def_output}"

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

echo "Copilot CLI -> ${COPILOT_PROVIDER_BASE_URL} (model: ${COPILOT_MODEL}, prompt<=${COPILOT_PROVIDER_MAX_PROMPT_TOKENS}, output<=${COPILOT_PROVIDER_MAX_OUTPUT_TOKENS})" >&2
exec copilot "$@"

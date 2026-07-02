#!/usr/bin/env bash
# Launch GitHub Copilot CLI against the local LiteLLM gateway (BYOK).
# Docs: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-byok-models
#
# Reads LITELLM_MASTER_KEY from /srv/ai/docker/.env (gitignored) so no secret is
# hardcoded. Override the model with:  COPILOT_MODEL=chat copilot-byok.sh
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

echo "Copilot CLI -> ${COPILOT_PROVIDER_BASE_URL} (model: ${COPILOT_MODEL})" >&2
exec copilot "$@"

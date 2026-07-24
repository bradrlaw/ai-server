#!/usr/bin/env bash
# Reusable output-quality benchmark harness (lm-evaluation-harness) for the AI server.
#
# Drives ANY OpenAI-compatible chat endpoint (llama-swap :9090, the pxq_llama fork
# server, LiteLLM :4000, ...) with generative tasks. Generative tasks are used
# because llama.cpp's server does not return prompt-token logprobs (needed by
# loglikelihood tasks like winogrande/hellaswag), and because gemma-4's batched
# eval path is broken in our llama.cpp build while its server generation is correct.
#
# Standard task: gsm8k (5-shot CoT). Optional: mmlu_generative (broad knowledge).
#
# Usage:
#   scripts/lm-eval-run.sh <served-model> [base_url] [tasks] [limit] [max_gen_toks] [concurrency]
# Examples:
#   scripts/lm-eval-run.sh fast
#   scripts/lm-eval-run.sh fast-12b http://127.0.0.1:9090/v1/chat/completions gsm8k 200
#   scripts/lm-eval-run.sh qwen35-pxq4 http://127.0.0.1:8899/v1/chat/completions gsm8k 200 3072
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL="${1:?served model name required}"
BASE_URL="${2:-http://127.0.0.1:9090/v1/chat/completions}"
TASKS="${3:-gsm8k}"
LIMIT="${4:-200}"
MAXTOK="${5:-512}"          # bump to ~6144 for reasoning models (Qwen thinking phase)
CONC="${6:-4}"
# Extra gen_kwargs appended verbatim. For reasoning models pass "until=<|im_end|>"
# so the task's default stop strings (e.g. "Question:") don't fire inside the
# thinking phase and truncate before the answer reaches the content field.
EXTRA_GEN="${7:-}"
GEN="temperature=0,max_gen_toks=${MAXTOK}"
[ -n "$EXTRA_GEN" ] && GEN="${GEN},${EXTRA_GEN}"

STAMP="$(date +%Y%m%d)"
OUT="docs/data/lm-eval/${MODEL}-${TASKS//,/_}-${STAMP}"
mkdir -p "$OUT"

echo ">> lm-eval  model=$MODEL  tasks=$TASKS  limit=$LIMIT  gen=[$GEN]  url=$BASE_URL"
OPENAI_API_KEY=dummy venvs/lm-eval/bin/lm_eval \
  --model local-chat-completions \
  --model_args "model=${MODEL},base_url=${BASE_URL},num_concurrent=${CONC},max_retries=2,tokenized_requests=False,timeout=1200" \
  --tasks "$TASKS" \
  --limit "$LIMIT" \
  --apply_chat_template \
  --gen_kwargs "$GEN" \
  --output_path "$OUT" \
  --log_samples 2>&1 | tee "$OUT/run.log"

echo ">> results in $OUT"

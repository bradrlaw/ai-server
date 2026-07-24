#!/usr/bin/env bash
# GSM8K (5-shot, greedy) quality eval across the served-model roster using dedicated
# stock llama-server instances. Accuracy is parallelism-independent, so we run two
# workers (one per V100) in parallel to bound wall-time, then the dual-V100 models
# sequentially. `fast` (P100) auto-rewarms and is left alone; the two V100s are ours.
#
# Reasoning models (Qwen thinking / gemma reasoning-on): n=100, max_gen_toks=6144,
# until=<|im_end|>. Non-reasoning: n=200, max_gen_toks=512.
set -uo pipefail
cd "$(dirname "$0")/.."
export CUDA_DEVICE_ORDER=PCI_BUS_ID
BIN=src/llama.cpp/build/bin/llama-server
MODELS_DIR=/srv/ai/models
LOGDIR=/srv/ai/tmp/roster-eval
mkdir -p "$LOGDIR"

# name | file | reasoning(1/0) | kv | parallel | ctx
declare -A SPEC=(
 [coding]="qwen3.6-27b-mtp/Qwen3.6-27B-Q6_K.gguf|1|q8_0|6|49152"
 [chat]="qwen3.6-35b-a3b-mtp/Qwen3.6-35B-A3B-UD-Q6_K.gguf|1|q8_0|6|49152"
 [chat-uncensored-q6]="qwen3.6-35b-a3b/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q6_K.gguf|1|q8_0|6|49152"
 [fast-uncensored]="gemma-4-12b-uncensored/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf|1|f16|6|49152"
 [gemma-31b]="gemma-4-31b/gemma-4-31B-it-qat-UD-Q4_K_XL.gguf|0|q8_0|8|16384"
 [big]="qwen3.6-27b-mtp/Qwen3.6-27B-UD-Q6_K_XL.gguf|1|f16|6|49152"
 [coder-next]="qwen3-coder-next/Qwen3-Coder-Next-UD-Q4_K_XL.gguf|0|f16|8|16384"
)

run_one() {  # $1=name  $2=gpus  $3=port
  local NAME=$1 GPUS=$2 PORT=$3
  IFS='|' read -r FILE REASON KV PAR CTX <<<"${SPEC[$NAME]}"
  local URL="http://127.0.0.1:${PORT}/v1/chat/completions"
  echo "[$NAME] launching on gpus=$GPUS port=$PORT (reason=$REASON kv=$KV par=$PAR ctx=$CTX)"
  local ARGS=(--model "$MODELS_DIR/$FILE" --ctx-size "$CTX" --parallel "$PAR"
        --batch-size 2048 --ubatch-size 1024
        --host 127.0.0.1 --port "$PORT" --n-gpu-layers 999
        --flash-attn on --cont-batching --jinja)
  [ "$KV" = "q8_0" ] && ARGS+=(--cache-type-k q8_0 --cache-type-v q8_0)
  [ "$REASON" = "0" ] && ARGS+=(--reasoning-budget 0)
  CUDA_VISIBLE_DEVICES="$GPUS" "$BIN" "${ARGS[@]}" >"$LOGDIR/$NAME.server.log" 2>&1 &
  local SPID=$!
  local ok=0
  for i in $(seq 1 240); do
    kill -0 "$SPID" 2>/dev/null || { echo "[$NAME] server died"; break; }
    if curl -sf "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"ok"'; then ok=1; break; fi
    sleep 2
  done
  if [ "$ok" != 1 ]; then
    echo "[$NAME] FAILED TO LOAD"; tail -25 "$LOGDIR/$NAME.server.log"; kill "$SPID" 2>/dev/null; sleep 3; return 1
  fi
  echo "[$NAME] ready, running harness"
  if [ "$REASON" = 1 ]; then
    scripts/lm-eval-run.sh "$NAME" "$URL" gsm8k 100 6144 6 "until=<|im_end|>" >"$LOGDIR/$NAME.eval.log" 2>&1
  else
    scripts/lm-eval-run.sh "$NAME" "$URL" gsm8k 200 512 8 >"$LOGDIR/$NAME.eval.log" 2>&1
  fi
  echo "[$NAME] harness done; killing server $SPID"
  kill "$SPID" 2>/dev/null; sleep 5
}

worker() {  # $1=gpus $2=port  $3..=model names
  local GPUS=$1 PORT=$2; shift 2
  for m in "$@"; do run_one "$m" "$GPUS" "$PORT"; done
  echo "WORKER gpus=$GPUS done"
}

echo "### unloading llama-swap to free both V100s"
curl -s 127.0.0.1:9090/unload >/dev/null 2>&1; sleep 4

echo "### phase 1: two single-card workers in parallel (idx1, idx2)"
worker 1 8899 coding chat-uncensored-q6 gemma-31b &
WA=$!
worker 2 8900 chat fast-uncensored &
WB=$!
wait $WA; wait $WB
echo "### phase 1 complete"

echo "### phase 2: dual-V100 models (sequential)"
curl -s 127.0.0.1:9090/unload >/dev/null 2>&1; sleep 4
run_one big "1,2" 8899
run_one coder-next "1,2" 8899

echo "### ALL DONE"

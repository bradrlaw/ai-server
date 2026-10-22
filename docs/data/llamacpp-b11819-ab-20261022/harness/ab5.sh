#!/usr/bin/env bash
# llama.cpp A/B runner. A = production b9850 build, B = test build b11819. Throwaway, untracked.
# Needs the benchmark window to be open (window.sh open). Usage: ab.sh PHASE [PHASE...]
#   smoke | lb-coding | lb-chat | lb-big | mtp-coding | mtp-chat | mtp-big | ctxfit | kld | all
# Create $RUN/STOP to make the runner exit cleanly before its next step.
set -uo pipefail
cd /srv/ai
export CUDA_DEVICE_ORDER=PCI_BUS_ID
HERE=/srv/ai/tmp/lcpp-test
RUN=${RUN:-$HERE/ab1}
mkdir -p "$RUN/csv" "$RUN/logs" "$RUN/lb" "$RUN/kld"
export MTP_DATA_DIR="$RUN/csv"

A=/srv/ai/src/llama.cpp/build/bin
B=/srv/ai/src/llama.cpp-b11819/build/bin
M_CODING=/srv/ai/models/qwen3.8-27b/Qwen3.8-27B-Q6_K.gguf
M_CHAT=/srv/ai/models/qwen3.6-35b-a3b-mtp/Qwen3.6-35B-A3B-UD-Q6_K.gguf
M_BIG=/srv/ai/models/qwen3.8-27b/Qwen3.8-27B-UD-Q8_K_XL.gguf
M_SMALL=/srv/ai/models/gemma-4-12b/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf
WIKI=/srv/ai/src/llama.cpp/wikitext-2-raw/wiki.test.raw

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$RUN/runner.log"; }
bin_of() { [[ $1 == A ]] && echo "$A" || echo "$B"; }

guard() {
  local st; st=$("$HERE/window.sh" status 2>&1)
  if [[ $st != OPEN* ]]; then log "ABORT: benchmark window is not open"; exit 3; fi
  if [[ -e $RUN/STOP ]]; then log "STOP file present, exiting"; exit 0; fi
}

# Wait until the given GPUs hold < 1500 MiB (idle ComfyUI contexts are ~310 MiB each).
gpus_free() {
  local g used deadline=$((SECONDS + 150))
  for g in "$@"; do
    while :; do
      used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$g" | tr -d ' ')
      [[ $used -lt 1500 ]] && break
      if (( SECONDS > deadline )); then log "ABORT step: GPU $g still holds $used MiB"; return 1; fi
      sleep 3
    done
  done
}

# mtp LABEL BUILD GPUS mtp-bench-args...
mtp() {
  local label=$1 build=$2 gpus=$3; shift 3
  guard; gpus_free ${gpus//,/ } || return 1
  log "mtp-bench $label [$build]: $*"
  rm -f /tmp/mtp-bench-*.log
  LLAMA_SERVER_BIN="$(bin_of "$build")/llama-server" python3 scripts/mtp-bench.py --no-restore --label "$label" "$@" \
    > "$RUN/logs/$label.txt" 2>&1
  local rc=$?
  mkdir -p "$RUN/logs/$label.srv"; cp /tmp/mtp-bench-*.log "$RUN/logs/$label.srv/" 2>/dev/null
  log "  rc=$rc; $(grep -c 'decode' "$RUN/logs/$label.txt") result lines"
  return $rc
}

abba() {  # abba BASE GPUS args...  -> A1 B1 B2 A2
  local base=$1 gpus=$2 s; shift 2
  for s in A1 B1 B2 A2; do mtp "$base-$s" "${s:0:1}" "$gpus" "$@" || log "  (step failed, continuing)"; done
}
ab() {    # ab BASE GPUS args...    -> A1 B1
  local base=$1 gpus=$2 s; shift 2
  for s in A1 B1; do mtp "$base-$s" "${s:0:1}" "$gpus" "$@" || log "  (step failed, continuing)"; done
}

# lbench LABEL BUILD CVD MODEL llama-bench-args...
lbench() {
  local label=$1 build=$2 cvd=$3 model=$4; shift 4
  guard; gpus_free ${cvd//,/ } || return 1
  log "llama-bench $label [$build]"
  CUDA_VISIBLE_DEVICES=$cvd "$(bin_of "$build")/llama-bench" -m "$model" -ngl 99 -fa on -p 512 -n 128 -d 0,8192 -r 3 -o csv "$@" \
    > "$RUN/lb/$label.csv" 2> "$RUN/lb/$label.err"
  log "  rc=$?"
}
labba() { local base=$1 cvd=$2 model=$3 s; shift 3; for s in A1 B1 B2 A2; do lbench "$base-$s" "${s:0:1}" "$cvd" "$model" "$@"; done; }
lab()   { local base=$1 cvd=$2 model=$3 s; shift 3; for s in A1 B1; do lbench "$base-$s" "${s:0:1}" "$cvd" "$model" "$@"; done; }

# kld LABEL MODEL CVD extra-args...   (A saves the base logits, B compares against them)
kld() {
  local label=$1 model=$2 cvd=$3; shift 3
  guard; gpus_free ${cvd//,/ } || return 1
  local common=(-m "$model" -f "$WIKI" -c 2048 --chunks 8 -b 512 -ub 512 -ngl 999 -fa on "$@")
  log "kld $label: A saves logits"
  CUDA_VISIBLE_DEVICES=$cvd "$A/llama-perplexity" "${common[@]}" --kl-divergence-base "$RUN/kld/$label.logits" > "$RUN/kld/$label.A.txt" 2>&1
  log "  A rc=$?; size $(du -h "$RUN/kld/$label.logits" 2>/dev/null | cut -f1)"
  guard; gpus_free ${cvd//,/ } || return 1
  log "kld $label: B compares"
  CUDA_VISIBLE_DEVICES=$cvd "$B/llama-perplexity" "${common[@]}" --kl-divergence-base "$RUN/kld/$label.logits" --kl-divergence > "$RUN/kld/$label.B.txt" 2>&1
  log "  B rc=$?"
  rm -f "$RUN/kld/$label.logits"
}

SIZES=512,4096,16384
phase() {
  case $1 in
    smoke)
      mtp smoke-B B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 3 --prompt code --sizes 512 --gen 64 --temp 0.0 ;;
    lb-coding) labba coding 1 $M_CODING -ctk q8_0 -ctv q8_0 -b 2048 -ub 2048 ;;
    lb-chat)   labba chat   2 $M_CHAT   -ctk q8_0 -ctv q8_0 -b 2048 -ub 2048 ;;
    lb-big)    lab   big    1,2 $M_BIG  -ctk f16 -ctv f16 -sm layer ;;
    mtp-coding)
      abba coding-code 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 0 3 --prompt code --sizes $SIZES --gen 256 --temp 0.0
      abba coding-sum  1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 3 --prompt summary --sizes $SIZES --gen 256 --temp 0.6 ;;
    mtp-chat)
      abba chat-sum  2 --model $M_CHAT --gpu 2 --kv q8_0 --ctx 40960 --nmax 0 2 --prompt summary --sizes $SIZES --gen 256 --temp 0.6
      ab   chat-code 2 --model $M_CHAT --gpu 2 --kv q8_0 --ctx 40960 --nmax 2 --prompt code --sizes $SIZES --gen 256 --temp 0.0 ;;
    mtp-big)
      ab big-code 1,2 --model $M_BIG --gpu 1,2 --split --kv f16 --ubatch 512 --ctx 40960 --nmax 0 2 --prompt code --sizes $SIZES --gen 256 --temp 0.0 ;;
    ctxfit)
      for s in A B; do
        mtp ctxfit-coding-$s $s 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 163840 --fill 150000 --nmax 3 --gen 128 --temp 0.0
        mtp ctxfit-chat-$s   $s 2 --model $M_CHAT   --gpu 2 --kv q8_0 --ctx 98304  --fill 90000  --nmax 2 --gen 128 --temp 0.0
      done ;;
    kld)
      kld coding $M_CODING 1 -ctk q8_0 -ctv q8_0
      kld chat   $M_CHAT   2 -ctk q8_0 -ctv q8_0
      kld big    $M_BIG    1,2 -ctk f16 -ctv f16 -sm layer ;;
    small)  lab small 0 $M_SMALL -ctk f16 -ctv f16 -b 2048 -ub 2048 ;;
    e2)
      for s in B1 B2; do
        mtp coding-sumE2-$s B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 3 --prompt summary --sizes $SIZES --gen 256 --temp 0.6 --extra "--spec-draft-sampling probabilistic"
      done
      for s in B1 B2; do
        mtp chat-sumE2-$s B 2 --model $M_CHAT --gpu 2 --kv q8_0 --ctx 40960 --nmax 2 --prompt summary --sizes $SIZES --gen 256 --temp 0.6 --extra "--spec-draft-sampling probabilistic"
      done ;;
    e10)
      for s in B1 B2; do
        GGML_CUDA_GRAPH_OPT=1 mtp coding-sumE10-$s B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 3 --prompt summary --sizes $SIZES --gen 256 --temp 0.6
      done ;;
    e5)
      mtp coding-sumN-B1 B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 2 4 5 --prompt summary --sizes $SIZES --gen 256 --temp 0.6
      mtp coding-codeN-B1 B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 2 4 5 --prompt code --sizes $SIZES --gen 256 --temp 0.0
      mtp coding-sumP5-B1 B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 3 --prompt summary --sizes $SIZES --gen 256 --temp 0.6 --extra "--spec-draft-p-min 0.5"
      mtp coding-sumBS-B1 B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 3 --prompt summary --sizes $SIZES --gen 256 --temp 0.6 --extra "--no-spec-draft-backend-sampling" ;;
    e6)
      guard; gpus_free 1 || return 1
      for s in A B; do
        python3 "$HERE/e6-ttft.py" --build $s --label e6-$s --gpu 1 --out "$RUN/e6" > "$RUN/e6-$s.txt" 2>&1; log "e6 $s rc=$?"
        gpus_free 1 || return 1
      done ;;
    kldctl)
      guard; gpus_free 2 || return 1
      local c=(-m "$M_CHAT" -f "$WIKI" -c 2048 --chunks 8 -ngl 999 -fa on -ctk q8_0 -ctv q8_0)
      log "kldctl: A ub512 saves base"
      CUDA_VISIBLE_DEVICES=2 "$A/llama-perplexity" "${c[@]}" -b 512 -ub 512 --kl-divergence-base "$RUN/kld/ctl.logits" > "$RUN/kld/ctl-A512.txt" 2>&1
      log "kldctl: A ub256 vs A ub512 (same build, different kernel selection)"
      CUDA_VISIBLE_DEVICES=2 "$A/llama-perplexity" "${c[@]}" -b 256 -ub 256 --kl-divergence-base "$RUN/kld/ctl.logits" --kl-divergence > "$RUN/kld/ctl-A256-vs-A512.txt" 2>&1
      log "kldctl: B ub256 vs A ub512"
      CUDA_VISIBLE_DEVICES=2 "$B/llama-perplexity" "${c[@]}" -b 256 -ub 256 --kl-divergence-base "$RUN/kld/ctl.logits" --kl-divergence > "$RUN/kld/ctl-B256-vs-A512.txt" 2>&1
      rm -f "$RUN/kld/ctl.logits" ;;
    small2)
      lbench small-B2 B 0 $M_SMALL -ctk f16 -ctv f16 -b 2048 -ub 2048
      lbench small-A2 A 0 $M_SMALL -ctk f16 -ctv f16 -b 2048 -ub 2048 ;;
    bs)
      mtp coding-sumBS-B1 B 1 --model $M_CODING --gpu 1 --kv q8_0 --ctx 40960 --nmax 3 --prompt summary --sizes $SIZES --gen 256 --temp 0.6 --extra=--no-spec-draft-backend-sampling ;;
    ngchat)
      GGML_CUDA_DISABLE_GRAPHS=1 lbench chat-NG-B1 B 2 $M_CHAT -ctk q8_0 -ctv q8_0 -b 2048 -ub 2048 ;;
    ng)
      GGML_CUDA_DISABLE_GRAPHS=1 lbench coding-NG-B1 B 1 $M_CODING -ctk q8_0 -ctv q8_0 -b 2048 -ub 2048 ;;
    e10chat)
      for s in B1 B2; do
        GGML_CUDA_GRAPH_OPT=1 mtp chat-sumE10-$s B 2 --model $M_CHAT --gpu 2 --kv q8_0 --ctx 40960 --nmax 2 --prompt summary --sizes $SIZES --gen 256 --temp 0.6
      done ;;
    e6n)
      guard; gpus_free 1 || return 1
      for n in 4 5; do
        python3 "$HERE/e6-ttft.py" --build B --label e6-B-n$n --gpu 1 --out "$RUN/e6" --extra "--spec-draft-n-max $n" > "$RUN/e6-B-n$n.txt" 2>&1; log "e6 B n$n rc=$?"
        gpus_free 1 || return 1
      done ;;
    all)
      for p in smoke lb-coding mtp-coding lb-chat mtp-chat lb-big mtp-big kld ctxfit; do phase $p; done ;;
    *) echo "unknown phase $1"; exit 2 ;;
  esac
}

for p in "$@"; do log "=== phase $p"; phase "$p"; done
log "=== done: $*"

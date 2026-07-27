#!/usr/bin/env bash
# Matched eval: ThinkingCap-Qwen3.6-27B vs stock Qwen3.6-27B base (our coding file).
# Phase 1: two identical stock llama-servers (base idx1, ThinkingCap idx2), thinking ON,
#          greedy, run harness.py over 80 GSM8K items -> accuracy + reasoning-token count.
# Phase 2: relaunch each with MTP self-spec (--spec-type draft-mtp) --parallel 1, probe
#          decode t/s via /completion timings (drop-in coding-slot speed sanity).
# Self-contained: unloads llama-swap, cleans up servers, restores daily.
set -uo pipefail
cd /srv/ai
export CUDA_DEVICE_ORDER=PCI_BUS_ID
BIN=src/llama.cpp/build/bin/llama-server
BASE_MODEL=models/qwen3.6-27b-mtp/Qwen3.6-27B-Q6_K.gguf
TC_MODEL=models/thinkingcap-27b/ThinkingCap-Qwen3.6-27B-Q6_K.gguf
D=tmp/tc-eval
ITEMS=${ITEMS:-$D/gsm8k80.json}
mkdir -p "$D"

launch() { # card port model extra...
  local card=$1 port=$2 model=$3; shift 3
  CUDA_VISIBLE_DEVICES=$card "$BIN" --model "$model" \
    --host 127.0.0.1 --port "$port" --n-gpu-layers 999 --jinja "$@" \
    >"$D/server-$port.log" 2>&1 &
  echo $!
}
wait_health() { # port pid
  local port=$1 pid=$2 i
  for i in $(seq 1 150); do
    kill -0 "$pid" 2>/dev/null || { echo "SERVER $port DIED"; tail -40 "$D/server-$port.log"; return 1; }
    curl -sf 127.0.0.1:$port/health 2>/dev/null | grep -q '"ok"' && { echo "  :$port healthy (${i}x2s)"; return 0; }
    sleep 2
  done
  echo "  :$port LOAD TIMEOUT"; tail -40 "$D/server-$port.log"; return 1
}

echo "### unload llama-swap (free both V100s)"
curl -s 127.0.0.1:9090/unload >/dev/null 2>&1; sleep 3

echo "### PHASE 1: quality + thinking-token (parallel 4, ctx 40960, q8_0 KV)"
COMMON=(--ctx-size 40960 --parallel 4 --batch-size 2048 --ubatch-size 1024 --cache-type-k q8_0 --cache-type-v q8_0)
BPID=$(launch 1 8901 "$BASE_MODEL" "${COMMON[@]}")
TPID=$(launch 2 8902 "$TC_MODEL"   "${COMMON[@]}")
echo "base pid $BPID (idx1), thinkingcap pid $TPID (idx2)"
wait_health 8901 "$BPID" || { kill "$BPID" "$TPID" 2>/dev/null; exit 1; }
wait_health 8902 "$TPID" || { kill "$BPID" "$TPID" 2>/dev/null; exit 1; }
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader

echo "### running harness on stock base"
python3 "$D/harness.py" base-qwen3.6-27b 127.0.0.1:8901 "$ITEMS" "$D/base" 4 2>&1 | tee "$D/base.log"
echo "### running harness on ThinkingCap"
python3 "$D/harness.py" thinkingcap-27b 127.0.0.1:8902 "$ITEMS" "$D/thinkingcap" 4 2>&1 | tee "$D/thinkingcap.log"

echo "### killing phase-1 servers"
kill "$BPID" "$TPID" 2>/dev/null; sleep 4

echo "### PHASE 2: MTP decode-speed probe (--spec-type draft-mtp n3, parallel 1)"
SPEC=(--ctx-size 8192 --parallel 1 --batch-size 2048 --ubatch-size 2048 --cache-type-k q8_0 --cache-type-v q8_0 --spec-type draft-mtp --spec-draft-n-max 3)
probe_decode() { # label port
  local label=$1 port=$2
  # code-ish echo-light generative prompt; request 600 tokens, read timings
  curl -s 127.0.0.1:$port/completion -H 'Content-Type: application/json' \
    -d '{"prompt":"Write a detailed technical explanation of how a B-tree database index works, including insertion, splitting, and range scans. Be thorough.","n_predict":600,"temperature":0,"cache_prompt":false}' \
    | python3 -c "
import sys,json
d=json.load(sys.stdin); t=d.get('timings',{})
print(f'  $label decode: {t.get(\"predicted_per_second\"):.1f} t/s  (predicted {t.get(\"predicted_n\")} tok, pp {t.get(\"prompt_per_second\",0):.0f} t/s)')
"
}
for pair in "base-mtp 8901 $BASE_MODEL 1" "thinkingcap-mtp 8902 $TC_MODEL 2"; do
  set -- $pair; lbl=$1; port=$2; model=$3; card=$4
  PID=$(launch "$card" "$port" "$model" "${SPEC[@]}")
  if wait_health "$port" "$PID"; then
    probe_decode "$lbl" "$port"     # warm
    probe_decode "$lbl" "$port"     # measure
  fi
  kill "$PID" 2>/dev/null; sleep 4
done

echo "### restoring daily mode"
python3 scripts/llama-swap-mode.py set daily >/dev/null 2>&1 || true
echo "### DONE"
echo "=== base summary ===";        cat "$D/base.summary.json" 2>/dev/null
echo; echo "=== thinkingcap summary ==="; cat "$D/thinkingcap.summary.json" 2>/dev/null

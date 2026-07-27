#!/usr/bin/env bash
# Operating-point eval: ThinkingCap vs stock base at recommended sampler
# (temp 1.0, top_p 0.95, top_k 20), K samples/item, thinking ON.
set -uo pipefail
cd /srv/ai
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export TEMP=1.0 TOP_P=0.95 TOP_K=20 K=${K:-5}
BIN=src/llama.cpp/build/bin/llama-server
BASE_MODEL=models/qwen3.6-27b-mtp/Qwen3.6-27B-Q6_K.gguf
TC_MODEL=models/thinkingcap-27b/ThinkingCap-Qwen3.6-27B-Q6_K.gguf
D=tmp/tc-eval
ITEMS=${ITEMS:-$D/gsm8k40.json}
mkdir -p "$D"
launch(){ local card=$1 port=$2 model=$3; shift 3
  CUDA_VISIBLE_DEVICES=$card "$BIN" --model "$model" --host 127.0.0.1 --port "$port" \
    --n-gpu-layers 999 --jinja "$@" >"$D/server-$port.log" 2>&1 & echo $!; }
wait_health(){ local port=$1 pid=$2 i
  for i in $(seq 1 150); do kill -0 "$pid" 2>/dev/null || { echo "SERVER $port DIED"; tail -40 "$D/server-$port.log"; return 1; }
    curl -sf 127.0.0.1:$port/health 2>/dev/null | grep -q '"ok"' && { echo "  :$port healthy (${i}x2s)"; return 0; }
    sleep 2; done; echo "  :$port TIMEOUT"; tail -40 "$D/server-$port.log"; return 1; }
echo "### unload llama-swap"; curl -s 127.0.0.1:9090/unload >/dev/null 2>&1; sleep 3
COMMON=(--ctx-size 61440 --parallel 6 --batch-size 2048 --ubatch-size 1024 --cache-type-k q8_0 --cache-type-v q8_0)
BPID=$(launch 1 8901 "$BASE_MODEL" "${COMMON[@]}")
TPID=$(launch 2 8902 "$TC_MODEL"   "${COMMON[@]}")
echo "base pid $BPID (idx1), thinkingcap pid $TPID (idx2)"
wait_health 8901 "$BPID" || { kill "$BPID" "$TPID" 2>/dev/null; exit 1; }
wait_health 8902 "$TPID" || { kill "$BPID" "$TPID" 2>/dev/null; exit 1; }
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
echo "### harness: stock base (temp1)"
python3 "$D/harness.py" base-qwen3.6-27b 127.0.0.1:8901 "$ITEMS" "$D/base-t1" 6 2>&1 | tee "$D/base-t1.log"
echo "### harness: ThinkingCap (temp1)"
python3 "$D/harness.py" thinkingcap-27b 127.0.0.1:8902 "$ITEMS" "$D/thinkingcap-t1" 6 2>&1 | tee "$D/thinkingcap-t1.log"
echo "### killing servers"; kill "$BPID" "$TPID" 2>/dev/null; sleep 4
echo "### restoring daily"; python3 scripts/llama-swap-mode.py set daily >/dev/null 2>&1 || true
echo "### DONE"
echo "=== base-t1 ==="; cat "$D/base-t1.summary.json"; echo
echo "=== thinkingcap-t1 ==="; cat "$D/thinkingcap-t1.summary.json"

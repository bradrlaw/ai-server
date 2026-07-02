#!/usr/bin/env bash
# Benchmark Qwen3.6-35B-A3B (MoE) on the Tesla V100s: single card vs dual-card
# tensor split. No sudo required.
#
# Hardware notes (see /srv/ai/docs/server-setup.md):
#   nvidia-smi index order (PCI_BUS_ID): 0=P100, 1=V100, 2=V100
#   V100s have NO NVLink -> tensor split crosses PCIe gen3 (PHB).
#   Each V100 = 32 GB. Q6_K (~21 GB) fits ONE card; BF16 (~54 GB) needs BOTH.
#
# Usage: ./bench-qwen3.6-27b.sh [output_dir]
set -euo pipefail

MODEL_DIR=/srv/ai/models/qwen3.6-35b-a3b
BIN=/srv/ai/src/llama.cpp/build/bin/llama-bench
Q6K="$MODEL_DIR/Qwen3.6-35B-A3B-UD-Q6_K.gguf"
BF16="$MODEL_DIR/Qwen3.6-35B-A3B-BF16-00001-of-00002.gguf"   # sharded; loader finds shard 2

OUT_DIR="${1:-$MODEL_DIR/bench-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT_DIR"
RESULTS="$OUT_DIR/results.md"

# Align CUDA runtime ordering with nvidia-smi so device 1,2 == the two V100s.
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export PATH=/usr/local/cuda/bin:$PATH

# Bench params: prompt-processing (pp) and token-generation (tg) at two context
# depths to show how single-vs-dual scales with context length.
PARAMS=(-p 512 -n 128 -r 3 -ngl 99)
DEPTHS=(0 8192)

log() { echo -e "\n\033[1;36m==> $*\033[0m"; }

run() {  # run <label> <CUDA_VISIBLE_DEVICES> <model> <extra bench flags...>
  local label="$1" cvd="$2" model="$3"; shift 3
  log "$label  (CUDA_VISIBLE_DEVICES=$cvd)"
  echo -e "\n### $label\n" >>"$RESULTS"
  for d in "${DEPTHS[@]}"; do
    echo "  depth=$d ..."
    if ! CUDA_VISIBLE_DEVICES="$cvd" "$BIN" -m "$model" "${PARAMS[@]}" -d "$d" "$@" -o md \
      | tee -a "$RESULTS"; then
      echo "  (config failed — likely OOM — continuing)" | tee -a "$RESULTS"
    fi
  done
}

{
  echo "# Qwen3.6-35B-A3B (MoE) V100 benchmark"
  echo ""
  echo "- Date: $(date)"
  echo "- Driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  echo "- Bench params: ${PARAMS[*]} ; depths: ${DEPTHS[*]}"
} >"$RESULTS"

# --- Q6_K: fits one card -> compare single vs both split modes ---
if [[ -f "$Q6K" ]]; then
  run "Q6_K  single V100"          1   "$Q6K" -sm none
  run "Q6_K  dual V100 (layer)"    1,2 "$Q6K" -sm layer
  run "Q6_K  dual V100 (row)"      1,2 "$Q6K" -sm row
else
  echo "SKIP Q6_K (not found: $Q6K)" | tee -a "$RESULTS"
fi

# --- BF16: too big for one card -> dual only ---
if [[ -f "$BF16" ]]; then
  run "BF16  dual V100 (layer)"    1,2 "$BF16" -sm layer
  run "BF16  dual V100 (row)"      1,2 "$BF16" -sm row
else
  echo "SKIP BF16 (not found: $BF16)" | tee -a "$RESULTS"
fi

log "Done. Results -> $RESULTS"

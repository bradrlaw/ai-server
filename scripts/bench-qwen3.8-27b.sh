#!/usr/bin/env bash
# Benchmark Qwen3.8-27B (dense, qwen35 arch) on the Tesla V100s: single card vs
# dual-card tensor split. No sudo required. Mirrors bench-qwen3.6-27b.sh for the
# 3.8 migration (coding=Q6_K single-card, big=UD-Q6_K_XL dual-card).
#
# Hardware notes (see /srv/ai/docs/server-setup.md):
#   nvidia-smi index order (PCI_BUS_ID): 0=P100/TitanX, 1=V100, 2=V100
#   V100s have NO NVLink -> tensor split crosses PCIe gen3 (PHB).
#   Each V100 = 32 GB. Q6_K (~22 GB) fits ONE card; UD-Q6_K_XL (~26 GB) also fits
#   one card but is the `big` served weight (dual-card, long ctx) -> bench both.
#
# Usage: ./bench-qwen3.8-27b.sh [output_dir]
set -euo pipefail

MODEL_DIR=/srv/ai/models/qwen3.8-27b
BIN=/srv/ai/src/llama.cpp/build/bin/llama-bench
Q6K="$MODEL_DIR/Qwen3.8-27B-Q6_K.gguf"
UDXL="$MODEL_DIR/Qwen3.8-27B-UD-Q6_K_XL.gguf"

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
    CUDA_VISIBLE_DEVICES="$cvd" "$BIN" -m "$model" "${PARAMS[@]}" -d "$d" "$@" -o md \
      | tee -a "$RESULTS"
  done
}

{
  echo "# Qwen3.8-27B V100 benchmark"
  echo ""
  echo "- Date: $(date)"
  echo "- Driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  echo "- Bench params: ${PARAMS[*]} ; depths: ${DEPTHS[*]}"
} >"$RESULTS"

# --- Q6_K (coding slot): fits one card -> single vs both split modes ---
if [[ -f "$Q6K" ]]; then
  run "Q6_K  single V100"          1   "$Q6K" -sm none
  run "Q6_K  dual V100 (layer)"    1,2 "$Q6K" -sm layer
  run "Q6_K  dual V100 (row)"      1,2 "$Q6K" -sm row
else
  echo "SKIP Q6_K (not found: $Q6K)" | tee -a "$RESULTS"
fi

# --- UD-Q6_K_XL (big slot): fits one card, served dual -> single + dual ---
if [[ -f "$UDXL" ]]; then
  run "UD-Q6_K_XL  single V100"       1   "$UDXL" -sm none
  run "UD-Q6_K_XL  dual V100 (layer)" 1,2 "$UDXL" -sm layer
  run "UD-Q6_K_XL  dual V100 (row)"   1,2 "$UDXL" -sm row
else
  echo "SKIP UD-Q6_K_XL (not found: $UDXL)" | tee -a "$RESULTS"
fi

log "Done. Results -> $RESULTS"

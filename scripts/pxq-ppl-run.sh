#!/usr/bin/env bash
# Run the custom PXQ-capable perplexity tool over a set of Qwen3.6-35B-A3B
# quants — ALL on the pxq_llama fork engine with the identical algorithm — so
# the perplexity delta is purely the quantization. Q8_0 is the near-lossless
# reference. Appends results to docs/data/pxq/quality-ppl.csv.
#
# Usage: scripts/pxq-ppl-run.sh <chunks> [model_key ...]
#   model keys: q8 q6k pxq6 q4km pxq4   (default: all present)
set -uo pipefail
cd /srv/ai
FORK=/srv/ai/src/pxq_llama/pxq_llama-2026-07-23-linux-x64
NCCL=/srv/ai/venvs/comfyui/lib/python3.12/site-packages/nvidia/nccl/lib
export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1 PXA_ENHANCE=1 GGML_CUDA_NO_VMM=1
export LD_LIBRARY_PATH="$FORK/lib:/usr/local/cuda/lib64:$NCCL"
TOOL=scripts/bin/pxq-perplexity
CORPUS=src/llama.cpp/wikitext-2-raw/wiki.test.raw
CSV=docs/data/pxq/quality-ppl.csv
CHUNKS="${1:-100}"; shift || true

declare -A M=(
  [q8]="models/pxq/Qwen3.6-35B-A3B-Q8_0.gguf|Q8_0|reference (near-lossless)"
  [q6k]="models/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-UD-Q6_K.gguf|Q6_K|standard ~6.5bpw"
  [pxq6]="models/pxq/Qwen3.6-35B-A3B-PXQ6.gguf|PXQ6|fork ~5.3bpw"
  [q4km]="models/pxq/Qwen3.6-35B-A3B-Q4_K_M.gguf|Q4_K_M|standard ~4.8bpw"
  [pxq4]="models/pxq/Qwen3.6-35B-A3B-PXQ4.gguf|PXQ4|fork ~4.5bpw"
)
KEYS=("$@"); [ ${#KEYS[@]} -eq 0 ] && KEYS=(q8 q6k pxq6 q4km pxq4)
[ -f "$CSV" ] || echo "key,quant,label,ppl,chunks,ctx,predictions,size_gb" > "$CSV"
for k in "${KEYS[@]}"; do
  IFS='|' read -r path quant label <<< "${M[$k]}"
  if [ ! -f "$path" ]; then echo "SKIP $k ($path missing)"; continue; fi
  sz=$(du -b "$path" | cut -f1); szgb=$(awk "BEGIN{printf \"%.1f\", $sz/1073741824}")
  echo "=== $k $quant ($szgb GB) chunks=$CHUNKS ==="
  out=$(timeout 2400 $TOOL -m "$path" -f "$CORPUS" -c 512 --max-chunks "$CHUNKS" -ngl 999 2>/tmp/ppl-$k.log)
  echo "$out"
  ppl=$(echo "$out" | grep -oE "PPL [0-9.]+" | awk '{print $2}')
  preds=$(echo "$out" | grep -oE "predictions=[0-9]+" | cut -d= -f2)
  if [ -n "$ppl" ]; then
    echo "$k,$quant,$label,$ppl,$CHUNKS,512,$preds,$szgb" >> "$CSV"
  else
    echo "  !! no PPL parsed for $k (see /tmp/ppl-$k.log)"; tail -3 /tmp/ppl-$k.log
  fi
done
echo "=== CSV: $CSV ==="; column -t -s, "$CSV"

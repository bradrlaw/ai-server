#!/usr/bin/env bash
# Quantize our own BF16 models into PXQ tiers with the pxq_llama fork's llama-quantize.
set -u
FORK=/srv/ai/src/pxq_llama/pxq_llama-2026-07-22-linux-x64
export LD_LIBRARY_PATH="$FORK/bin:$FORK/src:$FORK/ggml/src:$FORK/examples/mtmd:/usr/local/cuda/lib64:/srv/ai/venvs/comfyui/lib/python3.12/site-packages/nvidia/nccl/lib"
export CUDA_DEVICE_ORDER=PCI_BUS_ID
OUT=/srv/ai/models/pxq
mkdir -p "$OUT"
Q="$FORK/bin/llama-quantize"
NTHREADS=16
quant() {  # src out tier
  local src="$1" out="$2" tier="$3"
  echo "=== $(date +%H:%M:%S) $tier -> $(basename "$out") ==="
  if [ -f "$out" ]; then echo "exists, skip"; return; fi
  "$Q" "$src" "$out" "$tier" "$NTHREADS" 2>&1 | tail -3
  ls -la "$out" 2>/dev/null | awk '{printf "  wrote %.2f GB\n",$5/1e9}'
}
SRC35=/srv/ai/models/qwen3.6-35b-a3b/Qwen3.6-35B-A3B-BF16-00001-of-00002.gguf
for T in PXQ6 PXQ4 PXQ3 PXQ2; do
  quant "$SRC35" "$OUT/Qwen3.6-35B-A3B-$T.gguf" "$T"
done
echo "=== ALL DONE $(date +%H:%M:%S) ==="
ls -la "$OUT"

#!/usr/bin/env bash
# Full pxq_llama vs stock benchmark matrix. Each line appends to docs/data/pxq/<target>.csv.
# Uses --no-restore between runs (keepalive-unloader keeps GPUs clear); restores daily at end.
set -u
cd /srv/ai
export CUDA_DEVICE_ORDER=PCI_BUS_ID
B="python3 scripts/pxq-bench.py --no-restore"
run() { echo "########## $* ##########"; $B "$@" 2>&1 | tail -12; echo; }

# --- STOCK baselines (standard quant) ---
run --engine stock --target v100-qwen35-q6k
run --engine stock --target dualv100-qwen35-q6k

# --- FORK on self-made PXQ quants ---
run --engine fork --target v100-qwen35-q6k
run --engine fork --target v100-qwen35-pxq6
run --engine fork --target v100-qwen35-pxq4
run --engine fork --target v100-qwen35-pxq3
run --engine fork --target v100-qwen35-pxq2
run --engine fork --target p100-qwen35-pxq3
run --engine fork --target p100-qwen35-pxq2
run --engine fork --target dualv100-qwen35-pxq4

# --- MTP speculative decode on V100 (the reply's "fun decode numbers") ---
run --engine fork --target v100-qwen35-pxq4 --spec mtp:n_max=1

echo "@@@ MATRIX DONE $(date +%H:%M:%S) @@@"
python3 scripts/llama-swap-mode.py set daily >/dev/null 2>&1 && echo "daily restored"

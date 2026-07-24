#!/usr/bin/env bash
# Build the custom PXQ-capable perplexity tool. The pxq_llama fork is an
# ik_llama.cpp fork, so we compile against ik_llama.cpp's headers (NOT mainline
# llama.cpp — the struct ABI differs) and link the fork's PXQ-enabled shared
# libs. Clone once: git clone --depth 1 https://github.com/ikawrakow/ik_llama.cpp src/ik_llama_src
set -euo pipefail
FORK=/srv/ai/src/pxq_llama/pxq_llama-2026-07-23-linux-x64
IK=/srv/ai/src/ik_llama_src
OUT=/srv/ai/scripts/bin/pxq-perplexity
mkdir -p "$(dirname "$OUT")"
g++ -O2 -std=c++17 -I"$IK/include" -I"$IK/ggml/include" \
    /srv/ai/scripts/pxq-perplexity.cpp \
    -L"$FORK/lib" -lllama -lggml -Wl,-rpath,"$FORK/lib" -o "$OUT"
echo "built $OUT"

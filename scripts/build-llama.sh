#!/usr/bin/env bash
#
# build-llama.sh — clean CUDA build of llama.cpp for this server's GPUs.
#
# Targets Pascal (sm_60, P100) + Volta (sm_70, V100). Requires CUDA 12.x
# (see ADR-0003) — CUDA 13 cannot build for these cards.
#
set -euo pipefail

REPO=/srv/ai/src/llama.cpp
export PATH=/usr/local/cuda/bin:$PATH
export CUDACXX=/usr/local/cuda/bin/nvcc

cd "$REPO"

echo "==> Toolchain"
nvcc --version | grep release
cmake --version | head -1
command -v ccache >/dev/null && echo "    ccache: $(ccache --version | head -1)"
echo "    repo: $(git -C "$REPO" describe --tags 2>/dev/null || git -C "$REPO" rev-parse --short HEAD)"

echo "==> CLEAN: removing previous build artifacts"
rm -rf "$REPO/build"
find "$REPO" -maxdepth 2 -name CMakeCache.txt -delete 2>/dev/null || true

echo "==> Configure (CUDA on, arch 60;70, curl on, ccache on)"
cmake -S "$REPO" -B "$REPO/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="60;70" \
  -DLLAMA_CURL=ON \
  -DGGML_CCACHE=ON

echo "==> Build (using $(nproc) jobs)"
cmake --build "$REPO/build" --config Release -j "$(nproc)"

echo
echo "==> DONE. Key binaries:"
for b in llama-server llama-cli llama-bench llama-embedding; do
  [ -x "$REPO/build/bin/$b" ] && echo "    $REPO/build/bin/$b"
done
echo
echo "    Server: $REPO/build/bin/llama-server  (LLAMA_CURL=ON — can download models by URL)"

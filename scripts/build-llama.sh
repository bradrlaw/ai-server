#!/usr/bin/env bash
#
# build-llama.sh — clean, non-destructive CUDA build of llama.cpp for this server's GPUs.
#
# Arch superset 52;60;61;70 (ADR-0017): Maxwell sm_52 (the idx0 GTX Titan X
# stopgap), Pascal sm_60 (P100, for when one returns) + sm_61, Volta sm_70 (the
# V100s). Leaving out 52 breaks every idx0 model. Requires CUDA 12.x (ADR-0003);
# CUDA 13 cannot build for these cards.
#
# llama-swap launches build/bin/llama-server, so the live build keeps serving
# while this script:
#   1. applies the P100 FAST_FP16 carveout (ADR-0014) to the source if missing;
#   2. configures and builds from scratch into build.new/;
#   3. checks that the new llama-server runs, loads its libraries from its own
#      directory, and that libggml-cuda.so has code for every arch in ARCHS;
#   4. renames build/ to build.prev-<old commit>-<UTC time>/ and build.new/ to build/,
#      then re-checks the result (and swaps back if it fails).
# If anything fails before step 4, build/ is untouched. Models that are already
# loaded keep running the old binary until llama-swap next starts them. Only the
# newest build.prev-* is kept; hand-made build.bak-* copies are never touched.
#
# Rollback:  cd /srv/ai/src/llama.cpp && mv build build.bad && mv build.prev-<...> build
# Update:    cd /srv/ai/src/llama.cpp && git checkout -- ggml/src/ggml-cuda/common.cuh
#            && git pull   (drops the carveout first so the pull can't conflict),
#            then rerun this script, which re-applies it.
#
# Environment overrides:
#   ARCHS="52;60;61;70"  CUDA archs, numbers only (keep in sync with ADR-0017/0018)
#   SKIP_P100_PATCH=1    build without the carveout when it no longer applies upstream
#                        (it only matters on a P100; port it before one returns)
#
set -euo pipefail

REPO=/srv/ai/src/llama.cpp
LIVE="$REPO/build"
STAGE="$REPO/build.new"
PATCH=/srv/ai/scripts/patches/p100-fast-fp16-carveout.patch
ARCHS="${ARCHS:-52;60;61;70}"
export PATH=/usr/local/cuda/bin:$PATH
export CUDACXX=/usr/local/cuda/bin/nvcc

die() { echo "ERROR: $*" >&2; exit 1; }

# check_tree DIR: DIR/bin/llama-server runs and resolves the llama/ggml libraries
# from DIR/bin itself. Prints the version line; returns 1 with a reason otherwise.
check_tree() {
  local dir=$1 out bad
  out=$("$dir/bin/llama-server" --version 2>&1) \
    || { echo "llama-server --version failed: $out"; return 1; }
  bad=$(ldd "$dir/bin/llama-server" 2>&1 | grep -E 'libllama|libggml|libmtmd|not found' \
        | grep -vF "$dir/bin/" || true)
  [[ -z "$bad" ]] || { echo "libraries not loaded from $dir/bin: $bad"; return 1; }
  echo "${out%%$'\n'*}"
}

[[ "$ARCHS" =~ ^[0-9]+(;[0-9]+)*$ ]] || die "ARCHS must be numbers separated by ';' (got '$ARCHS')"

cd "$REPO"

# One run at a time: two would both write build.new/.
exec 9>/tmp/build-llama.lock
flock -n 9 || die "another build-llama.sh is already running"

echo "==> Toolchain"
nvcc --version | grep release
cmake --version | head -1
command -v ccache >/dev/null && echo "    ccache: $(ccache --version | head -1)"
echo "    repo: $(git describe --tags 2>/dev/null || git rev-parse --short HEAD)"
echo "    archs: $ARCHS"

echo "==> P100 FAST_FP16 carveout (ADR-0014)"
if git apply --reverse --check "$PATCH" 2>/dev/null; then
  echo "    already applied"
elif git apply --check "$PATCH" 2>/dev/null; then
  git apply "$PATCH"
  echo "    applied $PATCH"
elif [[ "${SKIP_P100_PATCH:-0}" == 1 ]]; then
  echo "    WARNING: it does not apply to this checkout; building WITHOUT it (SKIP_P100_PATCH=1)" >&2
else
  die "$PATCH does not apply to this checkout (upstream changed ggml/src/ggml-cuda/common.cuh).
       Port the 3-line change (ADR-0014) and refresh the .patch, or rerun with SKIP_P100_PATCH=1."
fi

echo "==> CLEAN: removing $STAGE (the live build/ is not touched)"
rm -rf "$STAGE"

echo "==> Configure into $STAGE (CUDA on, archs $ARCHS, ccache on)"
# CMAKE_BUILD_RPATH_USE_ORIGIN: binaries find their libraries via $ORIGIN instead
# of an absolute build.new/ path, so the tree still works after the rename.
cmake -S "$REPO" -B "$STAGE" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="$ARCHS" \
  -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON \
  -DGGML_CCACHE=ON

echo "==> Build (using $(nproc) jobs)"
cmake --build "$STAGE" --config Release -j "$(nproc)"

echo "==> Verify the new build (before it goes live)"
NEW_VER=$(check_tree "$STAGE") || die "new build is broken, live build untouched: $NEW_VER"
echo "    $NEW_VER"
ELF=$(cuobjdump --list-elf "$STAGE/bin/libggml-cuda.so" 2>&1) \
  || die "cuobjdump failed on libggml-cuda.so: $ELF"
HAVE=" $(grep -o 'sm_[0-9]*' <<<"$ELF" | sort -u | tr '\n' ' ' || true)"
for a in ${ARCHS//;/ }; do
  [[ "$HAVE" == *" sm_$a "* ]] || die "libggml-cuda.so has no sm_$a code (has:$HAVE), live build untouched"
done
echo "    CUDA code for:$HAVE"

echo "==> Swap into place"
PREV=""
if [[ -d "$LIVE" ]]; then
  OLD_OUT=$("$LIVE/bin/llama-server" --version 2>&1 || true)
  OLD_SHA=unknown
  [[ "$OLD_OUT" =~ \(([0-9a-f]+)\) ]] && OLD_SHA=${BASH_REMATCH[1]}
  PREV="$REPO/build.prev-$OLD_SHA-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$LIVE" "$PREV"
fi
mv "$STAGE" "$LIVE"
if ! LIVE_VER=$(check_tree "$LIVE"); then
  echo "ERROR: the new build fails from $LIVE: $LIVE_VER" >&2
  FAILED="$REPO/build.failed-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$LIVE" "$FAILED"
  [[ -n "$PREV" ]] && mv "$PREV" "$LIVE"
  die "swapped back; the failed tree is in $FAILED"
fi
echo "    live: $LIVE_VER"
if [[ -n "$PREV" ]]; then
  echo "    previous build kept for rollback: $PREV"
  for d in "$REPO"/build.prev-*; do
    [[ -d "$d" && "$d" != "$PREV" ]] || continue
    echo "    removing older rollback copy $d"
    rm -rf "$d"
  done
fi

echo
echo "==> DONE. Key binaries:"
for b in llama-server llama-cli llama-bench llama-embedding; do
  [ -x "$LIVE/bin/$b" ] && echo "    $LIVE/bin/$b"
done
echo
echo "    Loaded models keep the old binary until llama-swap next starts them."
[[ -n "$PREV" ]] && echo "    Rollback: cd $REPO && mv build build.bad && mv $(basename "$PREV") build"
exit 0

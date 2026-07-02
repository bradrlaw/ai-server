#!/usr/bin/env bash
#
# purge-cuda13.sh — cleanly remove CUDA Toolkit 13.3 from the AI server while
# PRESERVING the working nvidia-driver-580-server.
#
# CUDA 13 dropped Pascal (sm_60) + Volta (sm_70) support, so it cannot build for
# this machine's P100/V100 GPUs. We remove it here, then install CUDA 12.x next.
#
# Safe by design:
#   * Explicit package list (the 64 CUDA-13 pkgs verified via apt simulation).
#   * Aborts if any nvidia/driver/kernel package would be removed.
#   * Verifies nvidia-smi still works at the end.
#   * Keeps cuda-keyring so the NVIDIA apt repo stays available for CUDA 12.x.
#
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo bash $0" >&2
  exit 1
fi

PKGS=(
  cccl-13-3 cuda-command-line-tools-13-3 cuda-compiler-13-3 cuda-crt-13-3
  cuda-ctadvisor-13-3 cuda-cudart-13-3 cuda-cudart-dev-13-3 cuda-culibos-dev-13-3
  cuda-cuobjdump-13-3 cuda-cupti-13-3 cuda-cupti-dev-13-3 cuda-cuxxfilt-13-3
  cuda-documentation-13-3 cuda-driver-dev-13-3 cuda-gdb-13-3 cuda-libraries-13-3
  cuda-libraries-dev-13-3 cuda-nsight-compute-13-3 cuda-nsight-systems-13-3
  cuda-nvcc-13-3 cuda-nvdisasm-13-3 cuda-nvml-dev-13-3 cuda-nvprune-13-3
  cuda-nvrtc-13-3 cuda-nvrtc-dev-13-3 cuda-nvtx-13-3 cuda-opencl-13-3
  cuda-profiler-api-13-3 cuda-sandbox-dev-13-3 cuda-sanitizer-13-3
  cuda-tileiras-13-3 cuda-toolkit-13-3 cuda-toolkit-13-3-config-common
  cuda-toolkit-13-config-common cuda-toolkit-config-common cuda-tools-13-3
  cuda-visual-tools-13-3 gds-tools-13-3 libcublas-13-3 libcublas-dev-13-3
  libcufft-13-3 libcufft-dev-13-3 libcufile-13-3 libcufile-dev-13-3
  libcuobjclient-13-3 libcuobjclient-dev-13-3 libcurand-13-3 libcurand-dev-13-3
  libcusolver-13-3 libcusolver-dev-13-3 libcusparse-13-3 libcusparse-dev-13-3
  libnpp-13-3 libnpp-dev-13-3 libnvfatbin-13-3 libnvfatbin-dev-13-3
  libnvjitlink-13-3 libnvjitlink-dev-13-3 libnvjpeg-13-3 libnvjpeg-dev-13-3
  libnvptxcompiler-13-3 libnvvm-13-3 nsight-compute-2026.2.1 nsight-systems-2026.1.3
)

echo "==> Recording driver state BEFORE"
dpkg-query -W -f='${Package}\n' | grep -E '580-server|nvidia-dkms|nvidia-kernel' \
  | sort | tee /tmp/driver-before.txt
nvidia-smi -L || { echo "nvidia-smi failed BEFORE purge — aborting."; exit 1; }

# Only operate on packages actually installed (ignore any already-absent).
INSTALLED=()
for p in "${PKGS[@]}"; do
  if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
    INSTALLED+=("$p")
  fi
done
echo "==> ${#INSTALLED[@]} CUDA-13 packages to purge"

echo "==> SAFETY CHECK: simulate and ensure no nvidia/driver/kernel removal"
if apt-get -s purge "${INSTALLED[@]}" 2>&1 \
     | grep -E '^(Purg|Remv)' | grep -iE 'nvidia|580|kernel|dkms|firmware'; then
  echo "ABORT: simulation would remove a driver/kernel package. No changes made." >&2
  exit 1
fi
echo "    OK — no driver/kernel packages affected."

echo "==> Purging CUDA 13 ..."
DEBIAN_FRONTEND=noninteractive apt-get -y purge "${INSTALLED[@]}"
DEBIAN_FRONTEND=noninteractive apt-get -y autoremove --purge

echo "==> Removing leftover CUDA 13 directories / stale symlink"
rm -rf /usr/local/cuda-13 /usr/local/cuda-13.3
if [ -L /usr/local/cuda ] && [ ! -e /usr/local/cuda ]; then
  echo "    Removing dangling /usr/local/cuda symlink"
  rm -f /usr/local/cuda
fi

echo "==> Removing CUDA-13 linker config (keeping generic 000_cuda.conf)"
# These point at the now-removed cuda-13 / cuda-13.3 trees.
rm -f /etc/ld.so.conf.d/987_cuda-13.conf /etc/ld.so.conf.d/gds-13-3.conf
ldconfig
# Note: /etc/profile.d/cuda.sh and /etc/ld.so.conf.d/000_cuda.conf reference the
# generic /usr/local/cuda symlink and are intentionally LEFT in place; they will
# resolve correctly once CUDA 12.x is installed and the symlink repointed.

echo
echo "==> VERIFY driver intact AFTER"
dpkg-query -W -f='${Package}\n' | grep -E '580-server|nvidia-dkms|nvidia-kernel' \
  | sort | tee /tmp/driver-after.txt
if ! diff -q /tmp/driver-before.txt /tmp/driver-after.txt >/dev/null; then
  echo "WARNING: driver package set changed! Review /tmp/driver-before.txt vs -after.txt" >&2
else
  echo "    Driver package set unchanged ✅"
fi
nvidia-smi -L && echo "    nvidia-smi OK ✅"

echo "==> Remaining CUDA references (should be empty / none):"
ls -d /usr/local/cuda* 2>/dev/null || echo "    no /usr/local/cuda* dirs"
command -v nvcc >/dev/null && echo "    nvcc still on PATH: $(command -v nvcc)" \
  || echo "    nvcc gone from PATH (expected)"

echo
echo "==> DONE. CUDA 13 removed, driver preserved."
echo "    Next: install CUDA 12.x toolkit (e.g. cuda-toolkit-12-6) + cuDNN."

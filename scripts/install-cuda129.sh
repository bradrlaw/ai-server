#!/usr/bin/env bash
#
# install-cuda129.sh — install CUDA Toolkit 12.9 + cuDNN 9 on the AI server,
# WITHOUT touching the working nvidia-driver-580-server.
#
# CUDA 12.9 is the newest CUDA that still builds for Pascal (sm_60) and Volta
# (sm_70) — those were removed in CUDA 13. PyTorch ships stable cu129 wheels
# (torch 2.8–2.12), and llama.cpp/vLLM support 12.9.
#
# Safe by design:
#   * Installs the TOOLKIT-only metapackage (cuda-toolkit-12-9) — NOT the `cuda`
#     or `cuda-12-9` metapackage, so the 580 driver is never pulled/changed.
#   * Aborts if the install plan would remove/alter any nvidia/driver/kernel pkg.
#   * Sets /usr/local/cuda symlink, linker + PATH config, then verifies nvcc
#     targets sm_60 and sm_70.
#
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo bash $0" >&2
  exit 1
fi

CUDA_VER=12.9
PKGS=(cuda-toolkit-12-9 cudnn9-cuda-12 libcudnn9-dev-cuda-12)

echo "==> Recording driver state BEFORE"
dpkg-query -W -f='${Package}\n' | grep -E '580-server|nvidia-dkms|nvidia-kernel' \
  | sort | tee /tmp/driver-before-install.txt
nvidia-smi -L || { echo "nvidia-smi failed BEFORE install — aborting."; exit 1; }

echo "==> apt update"
apt-get update -qq

echo "==> SAFETY CHECK: simulate install, ensure no nvidia/driver/kernel change"
# Match against the package NAME only ($2) — the repo URL contains 'nvidia.com',
# so grepping whole lines would false-abort.
if apt-get -s install "${PKGS[@]}" 2>&1 \
     | awk '/^(Remv|Inst)/{print $2}' \
     | grep -iE 'nvidia|-580|kernel|dkms|firmware' \
     | grep -ivE '^cuda-driver-dev'; then
  echo "ABORT: install plan would touch a driver/kernel package. No changes made." >&2
  exit 1
fi
if apt-get -s install "${PKGS[@]}" 2>&1 | grep -qE '^Remv'; then
  echo "ABORT: install plan would REMOVE packages. Review manually." >&2
  apt-get -s install "${PKGS[@]}" 2>&1 | grep -E '^Remv'
  exit 1
fi
echo "    OK — toolkit-only, no driver/kernel/removal."

echo "==> Installing CUDA $CUDA_VER toolkit + cuDNN 9 ..."
DEBIAN_FRONTEND=noninteractive apt-get install -y "${PKGS[@]}"

echo "==> Pointing /usr/local/cuda -> cuda-$CUDA_VER"
if [ ! -d "/usr/local/cuda-$CUDA_VER" ]; then
  echo "ERROR: /usr/local/cuda-$CUDA_VER not found after install." >&2
  exit 1
fi
ln -sfn "/usr/local/cuda-$CUDA_VER" /usr/local/cuda
echo "    $(ls -ld /usr/local/cuda)"

echo "==> Linker config"
# Generic 000_cuda.conf (kept from before) points at /usr/local/cuda/targets/...
if [ ! -f /etc/ld.so.conf.d/000_cuda.conf ]; then
  echo "/usr/local/cuda/targets/x86_64-linux/lib" > /etc/ld.so.conf.d/000_cuda.conf
  echo "    recreated /etc/ld.so.conf.d/000_cuda.conf"
fi
ldconfig

echo "==> PATH config (/etc/profile.d/cuda.sh)"
if [ ! -f /etc/profile.d/cuda.sh ]; then
  cat > /etc/profile.d/cuda.sh <<'EOF'
export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
EOF
  echo "    created /etc/profile.d/cuda.sh"
else
  echo "    /etc/profile.d/cuda.sh already present (points at /usr/local/cuda)"
fi

echo
echo "==> VERIFY driver intact AFTER"
dpkg-query -W -f='${Package}\n' | grep -E '580-server|nvidia-dkms|nvidia-kernel' \
  | sort | tee /tmp/driver-after-install.txt
if diff -q /tmp/driver-before-install.txt /tmp/driver-after-install.txt >/dev/null; then
  echo "    Driver package set unchanged ✅"
else
  echo "WARNING: driver package set changed — review the two /tmp files." >&2
fi

echo
echo "==> VERIFY CUDA toolkit"
NVCC=/usr/local/cuda/bin/nvcc
"$NVCC" --version | sed -n '4,5p'
echo "--- nvcc supported GPU codes (must include sm_60 and sm_70) ---"
CODES=$("$NVCC" --list-gpu-code | tr '\n' ' ')
echo "    $CODES"
echo "$CODES" | grep -qw sm_60 && echo "    sm_60 (P100) supported ✅" || echo "    sm_60 MISSING ❌"
echo "$CODES" | grep -qw sm_70 && echo "    sm_70 (V100) supported ✅" || echo "    sm_70 MISSING ❌"

echo
echo "==> DONE. CUDA $CUDA_VER + cuDNN installed; 580 driver preserved."
echo "    Open a NEW shell (or: source /etc/profile.d/cuda.sh) so nvcc is on PATH."
echo "    Next: build llama.cpp with -DCMAKE_CUDA_ARCHITECTURES=\"60;70\"."

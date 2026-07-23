#!/usr/bin/env bash
# Recover from a poisoned NVIDIA UVM state (all NEW CUDA contexts fail with
# "ggml_cuda_init: failed to initialize CUDA: unknown error" / torch sees 0 devices)
# after a llama.cpp/fork GPU process segfaulted or was SIGKILLed mid-CUDA.
#
# Reloads nvidia_uvm WITHOUT a full reboot. Must run as root (uses systemctl + modprobe).
# Stops every process holding a CUDA context first (rmmod fails while the module is in use).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root:  sudo $0" >&2
  exit 1
fi

echo "== 1. Stop GPU-using services (they hold CUDA contexts) =="
systemctl stop comfyui-open.service comfyui-secure.service llama-swap.service || true
sleep 2

echo "== 2. Kill any remaining CUDA-context holders =="
mapfile -t PIDS < <(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | tr -d ' ')
if [ "${#PIDS[@]}" -gt 0 ]; then
  echo "  remaining: ${PIDS[*]}"
  kill "${PIDS[@]}" 2>/dev/null || true
  sleep 3
  kill -9 "${PIDS[@]}" 2>/dev/null || true
  sleep 2
fi

echo "== 3. Reload the nvidia_uvm module =="
rmmod nvidia_uvm
modprobe nvidia_uvm
echo "  nvidia_uvm reloaded"

echo "== 4. Restart services =="
systemctl start llama-swap.service comfyui-open.service comfyui-secure.service || true

echo "== 5. Sanity: a fresh CUDA context should now succeed =="
sleep 3
python3 - <<'PY' 2>/dev/null || echo "  (torch not importable here — verify with nvidia-smi + a llama-server load)"
import torch
print("  torch cuda:", torch.cuda.is_available(), "devices:", torch.cuda.device_count())
PY
echo "Done. Re-run the pxq benchmark once GPUs are healthy."

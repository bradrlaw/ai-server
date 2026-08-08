#!/usr/bin/env bash
# install-invokeai.sh — reproducible install of invoke-ai/InvokeAI on this box.
#
# WHY THIS SCRIPT EXISTS (the Volta caveats)
# ------------------------------------------
# InvokeAI is a full-featured, polished image-gen "studio" (unified canvas,
# layers, workflow graph, first-class model manager). Two things make a stock
# `pip install invokeai` fail on THIS server, both fixed here:
#
#   1. torch pin. InvokeAI's CURRENT main requires torch>=2.7 and its `cuda`
#      extra pins torch==2.7.1+cu128 — cu128 / torch>=2.7 DROP the sm_70 (Volta)
#      kernels our Tesla V100s need ("no kernel image is available"). So we pin
#      InvokeAI to **v5.11.0**, the newest release that still targets
#      `torch~=2.6.0`, and install the proven-good torch 2.6.0+cu124 (sm_70) that
#      ComfyUI/Fooocus/SwarmUI also use.
#
#   2. unpinned deps. v5.11.0's pyproject leaves transformers/pydantic/fastapi/
#      etc. loosely pinned; a 2026 index resolves them to future majors
#      (transformers 5.x, numpy 2.x, …) that break InvokeAI 5.11.0. We install
#      against a CONSTRAINTS file generated from InvokeAI's own v5.11.0 `uv.lock`
#      (the exact set they tested: transformers 4.50.3, diffusers 0.33.0,
#      numpy 1.26.4, pydantic 2.11.1, fastapi 0.115.12, …). The one lock pin that
#      is unavailable on PyPI, pypatchmatch==1.0.1 (yanked), is bumped to 1.0.2.
#
# Idempotent-ish: safe to re-run to repair/update the environment.
# Requires: git, python3.12, curl (all already present). No sudo.
set -euo pipefail

AI_ROOT="/srv/ai"
VENV="${AI_ROOT}/venvs/invokeai"
ROOT="${AI_ROOT}/invokeai"                     # runtime root (models DB, outputs, yaml)
INVOKE_VERSION="5.11.0"                         # last release on torch~=2.6.0 (sm_70)
TORCH_INDEX="https://download.pytorch.org/whl/cu124"
CONSTRAINTS_SRC="${AI_ROOT}/scripts/invokeai-constraints.txt"  # pins from v5.11.0 uv.lock

echo "==> 1/5 create venv (python3-venv is not installed system-wide, so use"
echo "        --without-pip + get-pip.py, matching the other tool venvs here)"
if [[ ! -x "${VENV}/bin/python" ]]; then
  python3 -m venv --without-pip "${VENV}"
  curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  "${VENV}/bin/python" /tmp/get-pip.py
  rm -f /tmp/get-pip.py
fi
"${VENV}/bin/pip" install --upgrade pip wheel setuptools

echo "==> 2/5 install the sm_70-capable torch (cu124 / 2.6.0), NOT InvokeAI's 2.7"
"${VENV}/bin/pip" install --no-cache-dir \
  torch==2.6.0 torchvision==0.21.0 --index-url "${TORCH_INDEX}"
# hard-verify Volta kernels are present before continuing
"${VENV}/bin/python" - <<'PY'
import torch
assert torch.__version__.startswith("2.6.0+cu124"), torch.__version__
assert "sm_70" in torch.cuda.get_arch_list(), torch.cuda.get_arch_list()
print("torch OK:", torch.__version__, "arch:", torch.cuda.get_arch_list())
PY

echo "==> 3/5 install InvokeAI ${INVOKE_VERSION} against the v5.11.0 lock constraints"
# torch is already installed and satisfies torch~=2.6.0, so pip leaves it alone
# (the cu124 build is NOT re-fetched from PyPI). The constraints file pins the
# rest of the tree to InvokeAI's tested versions.
if [[ ! -f "${CONSTRAINTS_SRC}" ]]; then
  echo "ERROR: missing ${CONSTRAINTS_SRC} (generated from InvokeAI v5.11.0 uv.lock)" >&2
  exit 1
fi
"${VENV}/bin/pip" install --no-cache-dir "invokeai==${INVOKE_VERSION}" -c "${CONSTRAINTS_SRC}"

# hf_transfer: the service sets HF_HUB_ENABLE_HF_TRANSFER=1 to accelerate InvokeAI's
# on-demand model/dependency downloads from the Hub. That flag REQUIRES this package
# or InvokeAI raises "hf_transfer package is not available" at model-load time. It is
# a self-contained Rust wheel with no Python deps (won't disturb the torch-2.6 pins).
"${VENV}/bin/pip" install --no-cache-dir "hf_transfer==0.1.9"

echo "==> 4/5 create the runtime root + config (host 0.0.0.0, port 9091, sdpa attn)"
mkdir -p "${ROOT}"
cp -f "${CONSTRAINTS_SRC}" "${ROOT}/constraints_v100.txt"
if [[ ! -f "${ROOT}/invokeai.yaml" ]]; then
  cat > "${ROOT}/invokeai.yaml" <<'YAML'
# InvokeAI configuration — AI server (Volta V100)
# Managed as part of the optional creative-tools install (see docs/optional-tools.md).
schema_version: 4.0.2

# Bind on all interfaces for LAN access (aipcub.local:9091), like ComfyUI/Fooocus/SwarmUI.
# Port 9091 avoids 9090 (llama-swap management endpoint).
host: 0.0.0.0
port: 9091

# GPU is pinned to a V100 via CUDA_VISIBLE_DEVICES in the systemd unit; device=auto
# then resolves to that single visible card. sm_70 has no fp8/FlashAttention, so use
# PyTorch scaled-dot-product attention (sdpa) — the correct Volta path.
device: auto
precision: auto
attention_type: torch-sdp
YAML
fi

echo "==> 5/5 verify invokeai-web starts and the API answers on :9091"
export INVOKEAI_ROOT="${ROOT}"
export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1
"${VENV}/bin/invokeai-web" --root "${ROOT}" > /tmp/invokeai-verify.log 2>&1 &
IPID=$!
ok=""
for i in $(seq 1 60); do
  sleep 3
  if curl -fsS "http://127.0.0.1:9091/api/v1/app/version" >/dev/null 2>&1; then ok=1; break; fi
  kill -0 "${IPID}" 2>/dev/null || { echo "invokeai-web exited early; see /tmp/invokeai-verify.log"; break; }
done
if [[ -n "${ok}" ]]; then
  echo "InvokeAI OK: $(curl -fsS http://127.0.0.1:9091/api/v1/app/version)"
fi
kill "${IPID}" 2>/dev/null || true
sleep 2
kill -9 "${IPID}" 2>/dev/null || true
[[ -n "${ok}" ]] || { echo "verification FAILED — check /tmp/invokeai-verify.log" >&2; exit 1; }

cat <<'EOF'

Done. To run the UI (port 9091, all interfaces):
  INVOKEAI_ROOT=/srv/ai/invokeai CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1 \
    /srv/ai/venvs/invokeai/bin/invokeai-web --root /srv/ai/invokeai

  as a service (needs sudo):
      sudo cp /srv/ai/scripts/invokeai.service /etc/systemd/system/
      sudo systemctl daemon-reload
      sudo systemctl enable --now invokeai
      sudo systemctl restart server-status   # pick up the status-page entry

NOTES
  - Pinned to InvokeAI v5.11.0 on purpose: it is the last release targeting
    torch~=2.6.0, which still has sm_70 (Volta) kernels. Newer InvokeAI requires
    torch>=2.7 (cu128), which will NOT run on the V100s. Do NOT `pip install -U`.
  - Host/port live in /srv/ai/invokeai/invokeai.yaml (0.0.0.0:9091).
  - GPU is pinned to a V100 (idx1) by the service via CUDA_VISIBLE_DEVICES=1.
  - Models: InvokeAI uses its own model manager / DB under the runtime root.
    Sharing weights with ComfyUI is a later step (InvokeAI imports models into
    its own store rather than reading a shared folder like SwarmUI does).
  - sm_70 has no fp8/FlashAttention; attention_type is set to torch-sdp (sdpa),
    the correct/only path for Volta.
EOF

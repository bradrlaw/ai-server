#!/usr/bin/env bash
# install-fooocus.sh — reproducible install of lllyasviel/Fooocus on this box.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# Fooocus is a simple, opinionated SDXL image-gen UI (a 2024-era ComfyUI fork
# under the hood, `ldm_patched`). Two things make a stock `python launch.py`
# fail on THIS server, both fixed here:
#
#   1. torch pin. Fooocus defaults to `torch==2.1.0` (see launch.py). That wheel
#      has NO cp312 build, and this box runs Python 3.12; even where it installs,
#      newer cu128/cu130 torch drops the sm_70(Volta)/sm_60(Pascal) kernels the
#      Tesla V100/P100 need. We install the SAME proven-good build ComfyUI runs
#      here — torch 2.6.0 + cu124 — whose kernel list includes sm_70/sm_60.
#
#   2. web stack. Fooocus ships `gradio==3.41.2` (2023) but leaves fastapi /
#      starlette / pydantic UNPINNED. On a 2026 package index pip resolves those
#      to versions that break gradio 3.41.2 at UI launch:
#         - starlette>=1.x -> Jinja2Templates "unhashable type: dict"
#         - pydantic>=2.5  -> fastapi 0.103 "FieldInfo has no attribute in_"
#      constraints_v100.txt pins the era-correct 2023 web stack so the UI boots.
#
# Idempotent-ish: safe to re-run to repair/update the environment.
# Requires: git, python3.12 (all already present). No sudo.
set -euo pipefail

AI_ROOT="/srv/ai"
REPO="${AI_ROOT}/Fooocus"
VENV="${AI_ROOT}/venvs/fooocus"
TORCH_INDEX="https://download.pytorch.org/whl/cu124"

echo "==> 1/5 clone (or update) the repo"
if [[ ! -d "${REPO}/.git" ]]; then
  git clone https://github.com/lllyasviel/Fooocus.git "${REPO}"
else
  git -C "${REPO}" pull --ff-only
fi

echo "==> 2/5 create venv (python3-venv is not installed system-wide, so use"
echo "        --without-pip + get-pip.py, matching the ComfyUI venv on this box)"
if [[ ! -x "${VENV}/bin/python" ]]; then
  python3 -m venv --without-pip "${VENV}"
  curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  "${VENV}/bin/python" /tmp/get-pip.py
  rm -f /tmp/get-pip.py
fi
"${VENV}/bin/pip" install --upgrade pip wheel setuptools

echo "==> 3/5 install the sm_70-capable torch (cu124 / 2.6.0), NOT Fooocus's 2.1.0"
"${VENV}/bin/pip" install --no-cache-dir \
  torch==2.6.0 torchvision==0.21.0 --index-url "${TORCH_INDEX}"
# hard-verify Volta kernels are present before continuing
"${VENV}/bin/python" - <<'PY'
import torch
assert torch.__version__.startswith("2.6.0+cu124"), torch.__version__
assert "sm_70" in torch.cuda.get_arch_list(), torch.cuda.get_arch_list()
print("torch OK:", torch.__version__, "arch:", torch.cuda.get_arch_list())
PY

echo "==> 4/5 write the Volta constraints + install Fooocus requirements"
cd "${REPO}"
cat > constraints_v100.txt <<'EOF'
# Volta (V100/P100 sm_70/sm_60) pin set for Fooocus 2.5.5 on Python 3.12.
# torch 2.6.0+cu124 is the proven-good build that still ships sm_70/sm_60 kernels
# (newer cu128/cu130 wheels drop Pascal/Volta). Fooocus default pins torch==2.1.0,
# which has no cp312 wheels, so we override it.
torch==2.6.0
torchvision==0.21.0
# Fooocus ships gradio==3.41.2 (2023) but leaves fastapi/starlette/pydantic UNPINNED.
# On a 2026 index pip resolves them to versions that break gradio 3.41.2:
#   - starlette>=1.x  -> Jinja2Templates "unhashable type: dict" at UI launch
#   - pydantic>=2.5   -> fastapi 0.103 "FieldInfo has no attribute in_"
# Pin the era-correct 2023 web stack that matches gradio 3.41.2.
fastapi==0.103.2
starlette==0.27.0
anyio==3.7.1
pydantic==2.4.2
pydantic-core==2.10.1
EOF
"${VENV}/bin/pip" install --no-cache-dir -r requirements_versions.txt -c constraints_v100.txt
# The web-stack pins are not in requirements_versions.txt, so install them explicitly.
"${VENV}/bin/pip" install --no-cache-dir \
  fastapi==0.103.2 starlette==0.27.0 anyio==3.7.1 pydantic==2.4.2 pydantic-core==2.10.1

echo "==> 5/5 verify Fooocus backend imports on torch 2.6"
"${VENV}/bin/python" - <<'PY'
import ldm_patched.modules.model_management as mm  # noqa: F401
import ldm_patched.modules.samplers, ldm_patched.modules.sd  # noqa: F401
import modules.core  # noqa: F401
print("Fooocus backend imports OK")
PY

cat <<'EOF'

Done. To run the UI (port 7865, loopback only):
  cd /srv/ai/Fooocus && CUDA_DEVICE_ORDER=PCI_BUS_ID \
    /srv/ai/venvs/fooocus/bin/python launch.py --port 7865 --listen 0.0.0.0

  as a service (needs sudo):
      sudo cp /srv/ai/scripts/fooocus.service /etc/systemd/system/
      sudo systemctl daemon-reload
      sudo systemctl enable --now fooocus

NOTES
  - Fooocus's ldm_patched default port is 8188 (== ComfyUI!). ALWAYS pass
    --port 7865 (the service does this) to avoid colliding with ComfyUI.
  - First real generation downloads the default SDXL model (~7GB) into
    Fooocus/models/checkpoints/. Pass --disable-preset-download to skip it,
    e.g. for a headless smoke test. Model-sharing with ComfyUI is a later step.
  - FIRST START IS SLOW: on the very first launch (empty models/checkpoints/)
    launch.py downloads that ~7GB checkpoint BEFORE it binds :7865, so the port
    stays closed and the status page shows Fooocus "down" until it finishes —
    minutes on a slow link. Subsequent starts are fast (model cached).
  - sm_70 has no fp8/FlashAttention-2; Fooocus uses pytorch cross attention
    (sdpa) on this box, which is the correct/only path for Volta.
EOF

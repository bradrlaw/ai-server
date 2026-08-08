#!/usr/bin/env bash
# install-ai-toolkit.sh — reproducible install of ostris/ai-toolkit on this box.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# ai-toolkit's own installer (the "AI Toolkit Manager", and its manual
# requirements) pins **torch 2.13.0 + cu130** (CUDA 13.0) aimed at Blackwell/
# sm120. CUDA 13.x wheels DROP the sm_70 (Volta) and sm_60 (Pascal) GPU kernels,
# so the stock install will not run on this server's Tesla V100/P100 — you'd get
# "CUDA error: no kernel image is available for execution on the device".
#
# This script installs the tool against the **cu124 / torch 2.6.0** wheel (the
# exact build ComfyUI already runs here, whose kernel list includes sm_70/sm_60),
# and reconciles the handful of torch-version-locked deps to that line.
#
# It is idempotent-ish: safe to re-run to repair/update the environment.
# Requires: git, python3.12, node>=20, npm (all already present). No sudo.
set -euo pipefail

AI_ROOT="/srv/ai"
REPO="${AI_ROOT}/ai-toolkit"
VENV="${AI_ROOT}/venvs/ai-toolkit"
TORCH_INDEX="https://download.pytorch.org/whl/cu124"

echo "==> 1/6 clone (or update) the repo"
if [[ ! -d "${REPO}/.git" ]]; then
  git clone --recurse-submodules https://github.com/ostris/ai-toolkit.git "${REPO}"
else
  git -C "${REPO}" pull --ff-only && git -C "${REPO}" submodule update --init --recursive
fi

echo "==> 2/6 create venv (python3-venv is not installed system-wide, so use"
echo "        --without-pip + get-pip.py, matching the ComfyUI venv on this box)"
if [[ ! -x "${VENV}/bin/python" ]]; then
  python3 -m venv --without-pip "${VENV}"
  curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  "${VENV}/bin/python" /tmp/get-pip.py
  rm -f /tmp/get-pip.py
fi
"${VENV}/bin/pip" install --upgrade pip wheel setuptools

echo "==> 3/6 install the sm_70-capable torch (cu124 / 2.6.0), NOT the cu130 pin"
"${VENV}/bin/pip" install --no-cache-dir \
  torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 --index-url "${TORCH_INDEX}"
# hard-verify Volta kernels are present before continuing
"${VENV}/bin/python" - <<'PY'
import torch, sys
assert torch.__version__.startswith("2.6.0+cu124"), torch.__version__
assert "sm_70" in torch.cuda.get_arch_list(), torch.cuda.get_arch_list()
print("torch OK:", torch.__version__, "arch:", torch.cuda.get_arch_list())
PY

echo "==> 4/6 reconcile torch-version-locked deps to the 2.6.0 line, then install"
cd "${REPO}"
# constraints: prevent any transitive dep from silently pulling a newer (sm_70-
# less) torch back in — the pollution trap.
cat > constraints_v100.txt <<'EOF'
torch==2.6.0
torchvision==0.21.0
torchaudio==2.6.0
EOF
# requirements_v100.txt = upstream base with only the torch-locked pins bumped
# down: torchao 0.10.0->0.9.0 and torchcodec 0.9.1->0.3.0 (the torch-2.6 releases).
sed -e 's/^torchao==0.10.0/torchao==0.9.0/' \
    -e 's/^torchcodec==0.9.1/torchcodec==0.3.0/' \
    requirements_base.txt > requirements_v100.txt
echo "scipy==1.12.0" >> requirements_v100.txt   # from upstream requirements.txt
"${VENV}/bin/pip" install --no-cache-dir -r requirements_v100.txt -c constraints_v100.txt

echo "==> 5/6 point the UI worker at our venv (it looks for TOOLKIT_ROOT/venv)"
ln -sfn "${VENV}" "${REPO}/venv"

echo "==> 6/6 build the web UI (node deps -> prisma sqlite db -> next build)"
cd "${REPO}/ui"
npm run install_deps
npm run update_db     # prisma generate + db push -> creates ../../aitk_db.db
npm run build         # tsc worker + next build

cat <<'EOF'

Done. To run the UI (port 8675):
  - one-off (foreground):  cd /srv/ai/ai-toolkit/ui && npm run start
  - as a service:          install scripts/ai-toolkit-ui.service (needs sudo):
      sudo cp /srv/ai/scripts/ai-toolkit-ui.service /etc/systemd/system/
      sudo systemctl daemon-reload
      sudo systemctl enable --now ai-toolkit-ui

NOTES
  - VIDEO/AUDIO training (Wan, MiniMax-H3, Ace-Step) additionally needs system
    FFmpeg for torchcodec:  sudo apt install ffmpeg
    (Image LoRA training — FLUX.1/SDXL/etc. — works without it; torchcodec is
    imported lazily only when a video/audio dataset is loaded.)
  - If exposing beyond loopback, set AI_TOOLKIT_AUTH in /srv/ai/ai-toolkit/ui.env
  - The very newest models (FLUX.2, cu130-era stacks) may not train on Volta;
    core FLUX.1 / SDXL / SD1.5 / Qwen-Image LoRA training is the supported path.
EOF

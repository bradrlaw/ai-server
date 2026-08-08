#!/usr/bin/env bash
# install-swarmui.sh — reproducible install of mcmonkeyprojects/SwarmUI on this box.
#
# WHAT SWARMUI IS
# --------------
# SwarmUI is a modular, multi-user AI image/video generation UI. It's a C#/.NET
# server that drives a Python **ComfyUI backend** under the hood, giving a friendly
# tabbed UI on top of Comfy's power, plus real user accounts/permissions. GPL-3.0,
# by Alex "mcmonkey" Goodwin (StabilityAI's Swarm lineage).
#
# WHY THIS SCRIPT EXISTS (the Volta caveats)
# ------------------------------------------
# Two traps on this V100/P100 (sm_70/sm_60) server, both handled here WITHOUT
# editing any SwarmUI file (so a future `git pull` of SwarmUI stays clean):
#
#   1. Backend torch. SwarmUI's ComfyUI-backend installer
#      (launchtools/comfy-install-linux.sh) pins torch from the **cu130** index,
#      which drops sm_70/sm_60 kernels -> "no kernel image" on our GPUs. BUT that
#      script installs torch WITHOUT `-U`, so if torch/torchvision/torchaudio are
#      ALREADY present in the backend venv, pip reports "already satisfied" and
#      skips the cu130 download. So we PRE-STAGE the backend venv with the proven
#      cu124 / torch 2.6.0 build (sm_70-capable) before SwarmUI's installer runs.
#
#   2. ComfyUI version. Latest ComfyUI master pulls `comfy-kitchen>=0.2.28`, whose
#      `na3d` custom op uses `list[int]` typing that torch 2.6's infer_schema
#      rejects (needs torch>=2.7, which has no sm_70). We pin the backend ComfyUI
#      to **v0.30.1** (the same tag our native ComfyUI runs, comfy-kitchen 0.2.26),
#      which imports cleanly on torch 2.6. SwarmUI's backend AutoUpdate defaults to
#      "false" and we sit on a detached tag, so it won't drift back to master.
#
# Requires: git, python3.12, wget, curl (all present). No sudo (dotnet installs
# user-local to ~/.dotnet).
set -euo pipefail

AI_ROOT="/srv/ai"
REPO="${AI_ROOT}/SwarmUI"
COMFY_TAG="v0.30.1"
TORCH_INDEX="https://download.pytorch.org/whl/cu124"
BK="${REPO}/dlbackend/ComfyUI"

echo "==> 1/6 clone (or update) SwarmUI"
if [[ ! -d "${REPO}/.git" ]]; then
  git clone https://github.com/mcmonkeyprojects/SwarmUI.git "${REPO}"
else
  git -C "${REPO}" pull --ff-only || true
fi

echo "==> 2/6 install user-local .NET 10 SDK (~/.dotnet), no sudo"
if ! "${HOME}/.dotnet/dotnet" --list-sdks 2>/dev/null | grep -q '^10\.0'; then
  wget -q https://dot.net/v1/dotnet-install.sh -O /tmp/dotnet-install.sh
  chmod +x /tmp/dotnet-install.sh
  /tmp/dotnet-install.sh --channel 10.0 --install-dir "${HOME}/.dotnet"
  rm -f /tmp/dotnet-install.sh
fi
export DOTNET_ROOT="${HOME}/.dotnet"; export PATH="${HOME}/.dotnet:${PATH}"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
dotnet --version

echo "==> 3/6 build SwarmUI (Release)"
cd "${REPO}"
dotnet build src/SwarmUI.csproj --configuration Release -o ./src/bin/live_release
git rev-parse HEAD > src/bin/last_build

echo "==> 4/6 pre-stage the ComfyUI backend on cu124 torch (sm_70-safe)"
mkdir -p "${REPO}/dlbackend"
if [[ ! -d "${BK}/.git" ]]; then
  git clone https://github.com/comfyanonymous/ComfyUI "${BK}"
fi
git -C "${BK}" fetch --tags -q origin || true
git -C "${BK}" checkout -q "${COMFY_TAG}"
if [[ ! -x "${BK}/venv/bin/python" ]]; then
  python3 -m venv --without-pip "${BK}/venv"
  curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  "${BK}/venv/bin/python" /tmp/get-pip.py
  rm -f /tmp/get-pip.py
fi
"${BK}/venv/bin/pip" install --upgrade pip wheel setuptools
# The three torch packages MUST all be present so SwarmUI's cu130 installer skips
# them (partial pre-stage lets it pull a cu130 torchaudio -> ABI mismatch).
"${BK}/venv/bin/pip" install --no-cache-dir \
  torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 --index-url "${TORCH_INDEX}"
"${BK}/venv/bin/python" - <<'PY'
import torch
assert torch.__version__.startswith("2.6.0+cu124"), torch.__version__
assert "sm_70" in torch.cuda.get_arch_list(), torch.cuda.get_arch_list()
print("backend torch OK:", torch.__version__)
PY
# ComfyUI requirements (torch pinned via constraints so nothing drags it up).
printf 'torch==2.6.0\ntorchvision==0.21.0\ntorchaudio==2.6.0\n' > /tmp/swarm-torch-c.txt
"${BK}/venv/bin/pip" install --no-cache-dir -r "${BK}/requirements.txt" -c /tmp/swarm-torch-c.txt
rm -f /tmp/swarm-torch-c.txt

echo "==> 5/6 verify the backend imports + boots on the V100"
( cd "${BK}" && CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1 \
    venv/bin/python -c "import torch, comfy.model_management as mm; \
      print('device', mm.get_torch_device(), torch.cuda.get_device_name(0))" )

echo "==> 6/6 done."
cat <<EOF

SwarmUI is built and the ComfyUI backend is pre-staged on cu124/torch-2.6 (sm_70),
pinned to ComfyUI ${COMFY_TAG}. Two steps remain (they need you, not the agent):

  1) Install + start the service (needs sudo):
       sudo cp ${AI_ROOT}/scripts/swarmui.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now swarmui
       # logs: journalctl -u swarmui -f

  2) Open http://<server>:7801 and complete the one-time install wizard:
       - pick your theme + network/authorization mode (SwarmUI has REAL multi-user
         accounts — choose accordingly),
       - Backend: **ComfyUI (self-starting)**  <-- important,
       - Models to predownload: **None** (we'll share ComfyUI's models later).
     Because the backend is pre-warmed, the wizard's ComfyUI step is fast and
     stays on cu124 (it will NOT re-download the cu130 torch).

The backend runs on a Tesla V100 (SwarmUI auto-picks the highest-VRAM GPU).
EOF

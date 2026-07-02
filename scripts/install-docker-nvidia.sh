#!/usr/bin/env bash
# Phase 1 (Foundation): install Docker CE + NVIDIA Container Toolkit for the
# containerized app tier (LiteLLM, Open WebUI, Qdrant, Postgres, Immich, ...).
# Native GPU engines (llama.cpp/llama-swap, vLLM, ComfyUI, whisper) stay native.
#
# Ubuntu 24.04, headless. Idempotent: safe to re-run.
#
# RUN WITH SUDO:  sudo /srv/ai/scripts/install-docker-nvidia.sh
#   Optional: pass the login user to add to the docker group (defaults to the
#   invoking sudo user):  sudo DOCKER_USER=brad /srv/ai/scripts/install-docker-nvidia.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

DOCKER_USER="${DOCKER_USER:-${SUDO_USER:-}}"

log() { echo -e "\n== $* =="; }

# ---------------------------------------------------------------------------
# 1. Docker CE from Docker's official apt repository
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker --version >/dev/null 2>&1; then
  log "Docker already installed: $(docker --version)"
else
  log "Installing Docker CE from the official Docker apt repo"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker
log "Docker: $(docker --version) | Compose: $(docker compose version | head -1)"

# ---------------------------------------------------------------------------
# 2. NVIDIA Container Toolkit from NVIDIA's official apt repository
# ---------------------------------------------------------------------------
if command -v nvidia-ctk >/dev/null 2>&1; then
  log "NVIDIA Container Toolkit already installed: $(nvidia-ctk --version | head -1)"
else
  log "Installing NVIDIA Container Toolkit"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y -qq nvidia-container-toolkit
fi

# ---------------------------------------------------------------------------
# 3. Wire the NVIDIA runtime into Docker + pin PCI bus ordering
# ---------------------------------------------------------------------------
log "Configuring the Docker runtime for NVIDIA"
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# ---------------------------------------------------------------------------
# 4. Add the login user to the docker group (rootless-ish convenience)
# ---------------------------------------------------------------------------
if [[ -n "$DOCKER_USER" ]] && id "$DOCKER_USER" >/dev/null 2>&1; then
  if id -nG "$DOCKER_USER" | tr ' ' '\n' | grep -qx docker; then
    log "User '$DOCKER_USER' already in the docker group"
  else
    log "Adding user '$DOCKER_USER' to the docker group (log out/in to take effect)"
    usermod -aG docker "$DOCKER_USER"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Validate: run nvidia-smi inside a CUDA container
# ---------------------------------------------------------------------------
log "Validating GPU access from a container (CUDA 12.9 base)"
if docker run --rm --gpus all -e CUDA_DEVICE_ORDER=PCI_BUS_ID \
     nvidia/cuda:12.9.2-base-ubuntu24.04 \
     nvidia-smi --query-gpu=index,name,power.limit --format=csv; then
  log "SUCCESS: containers can see all GPUs. Phase 1 foundation is ready."
else
  echo "!! GPU container validation FAILED. Check 'nvidia-ctk runtime configure' and 'docker info | grep -i runtime'."
  exit 1
fi

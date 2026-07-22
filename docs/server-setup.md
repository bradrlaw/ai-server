# Headless AI Server — Setup Notes & Plan

Personal headless AI server for development and AI workloads. Copilot CLI currently
runs directly on the box; eventually access will be via OpenAI-style API(s) only.

> **Design decisions** are recorded as ADRs in [`adr/`](adr/README.md).
> **Serving architecture** (topology, VRAM budget, routing table, rollout) is in
> [`architecture.md`](architecture.md).

Last updated: 2026-06-30

---

## Contents

- [1. Hardware](#1-hardware)
- [2. OS / Boot](#2-os--boot)
- [3. NVIDIA Driver — GOOD, keep it](#3-nvidia-driver--good-keep-it)
- [4. THE CUDA PROBLEM (primary task)](#4-the-cuda-problem-primary-task)
- [5. Per-GPU capability reality (drives model/engine choices)](#5-per-gpu-capability-reality-drives-modelengine-choices)
- [6. Inference engines — assessment](#6-inference-engines--assessment)
- [7. Strong recommendation: containerize the stack](#7-strong-recommendation-containerize-the-stack)
- [8. Intended workload split (user's plan — sound)](#8-intended-workload-split-users-plan--sound)
- [9. Additional considerations / things to verify](#9-additional-considerations--things-to-verify)
- [10. Proposed task order](#10-proposed-task-order)
- [Quick reference — current state (2026-06-30)](#quick-reference--current-state-2026-06-30)
- [Operator cheat-sheet — common commands](#operator-cheat-sheet--common-commands)
- [llama.cpp usage notes (learned during bring-up)](#llamacpp-usage-notes-learned-during-bring-up)
- [Benchmarks → see benchmarking.md](#benchmarks--see-benchmarkingmd)
- [GPU fan control (shroud fans) — runbook (2026-07-01)](#gpu-fan-control-shroud-fans--runbook-2026-07-01)
- [Phase 2 — llama-swap model router (2026-07-02)](#phase-2--llama-swap-model-router-2026-07-02)
- [Phase 2b — LiteLLM gateway (2026-07-02)](#phase-2b--litellm-gateway-2026-07-02)
- [GitHub Copilot CLI via BYOK (2026-07-02)](#github-copilot-cli-via-byok-2026-07-02)
- [Phase 3 — Open WebUI + SearXNG + mcpo (2026-07-02)](#phase-3--open-webui--searxng--mcpo-2026-07-02)
- [Phase 6 (partial) — ComfyUI generative media (2026-07-03)](#phase-6-partial--comfyui-generative-media-2026-07-03)
- [Personal-assistant gateways — OpenClaw + Hermes (2026-07-21)](#personal-assistant-gateways--openclaw--hermes-2026-07-21)
- [Network exposure & firewall (2026-07-07)](#network-exposure--firewall-2026-07-07)

## 1. Hardware

| Component    | Detail |
|--------------|--------|
| Motherboard  | MSI X99A Gaming Titanium Pro (LGA2011-3, X99 chipset) |
| CPU          | Intel Core i7-6950X (10C/20T, Broadwell-E, 40 PCIe lanes, ~140W TDP) |
| RAM          | 128 GB |
| Storage      | Intel 2 TB SSD (NVMe, `/dev/nvme0n1`) — installed in PCIe slot 4 |
| GPU 1        | NVIDIA Tesla V100-PCIE-32GB — slot 1 — compute cap **7.0** (Volta) |
| GPU 2        | NVIDIA Tesla V100-PCIE-32GB — slot 3 — compute cap **7.0** (Volta) |
| GPU 3        | NVIDIA Tesla P100-PCIE-16GB — slot 6 — compute cap **6.0** (Pascal) |

### GPU enumeration (Linux, driver 580)
- `GPU 0` = **P100** 16GB  (PCI 01:00.0)
- `GPU 1` = **V100** 32GB  (PCI 03:00.0)
- `GPU 2` = **V100** 32GB  (PCI 04:00.0)
- Total VRAM: 32 + 32 + 16 = **80 GB**

### Interconnect topology
- `nvidia-smi topo -m`: all pairs = **PHB** (PCIe via host bridge). **No NVLink**
  (these are PCIe V100s, not SXM2). Multi-GPU traffic crosses PCIe gen3 → the
  CPU host bridge. All GPUs currently negotiating **PCIe gen 3**.
- Implication: tensor parallelism across the two V100s is **PCIe-bandwidth bound**
  (no NVLink). Works, but inter-GPU comm is the bottleneck on long-context runs.

---

## 2. OS / Boot

- Primary OS: **Ubuntu 24.04.4 LTS**, kernel `6.8.0-124-generic`.
- Dual-boot with **Windows 11** (used to validate hardware: GPUs, fan control,
  LM Studio model tests on V100 + P100). System confirmed **stable under Windows**.
- Goal now: get the **Ubuntu** software stack correct.

### Installing Ubuntu + the NVIDIA driver from scratch

If you're reproducing this build on fresh hardware, start here before any of the
GPU/inference steps below. This box runs **Ubuntu Server 24.04 LTS** (headless).

1. **Install Ubuntu Server 24.04 LTS.** Download the ISO and follow Canonical's guide:
   - Download: <https://ubuntu.com/download/server>
   - Step-by-step tutorial (write USB → install → first boot):
     <https://ubuntu.com/tutorials/install-ubuntu-server>
   - During install, enable **OpenSSH server** for headless access; a minimal server
     install is fine (no desktop needed).

2. **Update the base system**, then reboot into the current kernel:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   sudo reboot
   ```

3. **Install the NVIDIA driver.** Use the Ubuntu-packaged driver (DKMS, auto-rebuilds
   on kernel updates). Full reference:
   <https://ubuntu.com/server/docs/nvidia-drivers-installation>
   ```bash
   ubuntu-drivers devices            # list recommended drivers for the detected GPUs
   sudo ubuntu-drivers install       # install the recommended driver, OR pin a version:
   sudo apt install -y nvidia-driver-580-server   # what this box runs (datacenter/headless)
   sudo reboot
   nvidia-smi                        # verify: all GPUs listed, driver 580.x
   ```
   Notes for this hardware:
   - Prefer the **`-server`** driver variant for headless Tesla cards (no desktop/X deps).
   - The driver is **decoupled from the CUDA toolkit** — install the driver here; the
     matching **CUDA 12.x** toolkit is handled separately in §4 (`scripts/install-cuda129.sh`).
   - Official NVIDIA driver downloads (if you need a version newer than Ubuntu ships):
     <https://www.nvidia.com/en-us/drivers/>

Everything from §3 onward assumes `nvidia-smi` works and all GPUs are detected.

---

## 3. NVIDIA Driver — GOOD, keep it

- Installed: **`nvidia-driver-580-server`** (580.159.03), DKMS.
- `nvidia-smi` works; all 3 GPUs detected correctly.
- **Driver and CUDA toolkit are decoupled.** The 580 driver is CUDA-13-capable and
  is fully backward compatible with CUDA 12.x toolkits. **Do NOT touch the driver.**

---

## 4. THE CUDA PROBLEM (primary task)

### Root cause — confirmed
A previous (ChatGPT-guided) install put **CUDA Toolkit 13.3** on the system:
- `/usr/local/cuda` → `cuda-13` → `cuda-13.3`
- `nvcc` 13.3 on PATH
- ~40 `cuda-*-13-3` apt packages installed via the `cuda-keyring` repo.

**CUDA 13 dropped support for Pascal and Volta.** Verified empirically:
`nvcc --list-gpu-code` on 13.3 lists only `sm_75, sm_80, sm_86, sm_87, sm_88,
sm_89, sm_90, sm_100, sm_103, sm_110, sm_120, sm_121`.
Our GPUs are **`sm_60` (P100)** and **`sm_70` (V100)** → **not buildable** with CUDA 13.

### Required fix
1. **Cleanly remove all CUDA 13 toolkit packages** (leave the 580 driver intact).
2. **Install CUDA 12.x toolkit** (12.x still supports sm_60 + sm_70). Recommended:
   **CUDA 12.6** (broad framework compatibility) or **12.8** (newer; good vLLM/torch
   wheel coverage). Avoid the `cuda` / `cuda-toolkit` *meta* package that drags in a
   driver — install the **versioned toolkit metapackage** `cuda-toolkit-12-6`
   (or `-12-8`) **only**, so the 580 driver is untouched.
3. Fix `/usr/local/cuda` symlink → `cuda-12.x`; set `PATH`/`LD_LIBRARY_PATH`.
4. Install **cuDNN for CUDA 12.x** (needed by some frameworks).

### Clean-uninstall outline (to be scripted, run with sudo)
- `apt-get --purge remove 'cuda*13*' 'libcudnn*cuda-13*' 'nsight*' ...` (driver pkgs
  `*-580-server` must be **excluded** from removal).
- `apt-get autoremove --purge`
- Remove leftover `/usr/local/cuda-13*` dirs and stale symlink.
- Verify driver still intact: `nvidia-smi` + `dpkg -l '*nvidia*580*'`.

> Note: `nvcc` not strictly required for llama.cpp/vLLM **if** using containers
> (see §7), but a host CUDA 12.x toolkit is the simplest path for native builds.

---

## 5. Per-GPU capability reality (drives model/engine choices)

| Feature                         | P100 (sm_60) | V100 (sm_70) |
|---------------------------------|:------------:|:------------:|
| Tensor Cores                    | ❌ none      | ✅ (FP16)    |
| FP16 compute                    | ✅           | ✅           |
| **BF16**                        | ❌           | ❌           |
| FP8                             | ❌           | ❌           |
| FlashAttention-2 (needs sm_80+) | ❌           | ❌           |
| Marlin AWQ/GPTQ kernels (sm_80+)| ❌           | ❌           |

**Consequences:**
- **No bf16** on either card → bf16-distributed weights must be cast to **fp16**.
- **No FlashAttention-2 / FP8 / Marlin** → many "fast path" vLLM kernels are
  unavailable; fall back to xformers/standard attention and fp16 or older quant kernels.
- **P100 has no Tensor Cores at all** and is being dropped by modern serving stacks.
  Best driven by **llama.cpp** (excellent Pascal support) for embeddings/TTS/STT.

---

## 6. Inference engines — assessment

### llama.cpp  (already cloned at `/srv/ai/src/llama.cpp`)
- **Best Volta/Pascal support of any engine.** GGUF quants, multi-GPU split, and an
  OpenAI-compatible `llama-server`. Build with `-DGGML_CUDA=ON` and arch
  `CMAKE_CUDA_ARCHITECTURES="60;70"`.
- Good first validation target. Likely the **primary** engine for V100 coding models
  too (Q4/Q5/Q6 GGUF lets a ~70B model fit across 2×V100=64GB).

### vLLM — evaluated and ABANDONED (2026-07-18)

**Do not pursue vLLM on this hardware.** A spike confirmed vLLM *can* run on a V100
(sm_70) with a pinned old release (`vllm==0.6.6.post1` + `transformers==4.47.1`,
XFormers backend, fp16, ~90 tok/s smoke; TP=2 ~+12–35% over TP=1 on Qwen2.5-7B) — but
that release predates every model in the roster (`qwen35`, `qwen3next`, `qwen35moe`,
new gemma), and the newer vLLM that supports them needs the sm_80-only V1 attention
backend. Volta also has no int8/FP8 quant kernels, so vLLM is fp16-only here (a 27B
can't fit one card and "Q8" isn't a real path). Net: vLLM can't serve a single one of
our daily models. **llama.cpp + llama-swap is the sole serving engine** until an
sm_80+ GPU is added. Full rationale + benchmark evidence: ADR-0004. Spike venv and the
Qwen2.5-7B test model were removed.

---

## 7. Strong recommendation: containerize the stack

Use **Docker + NVIDIA Container Toolkit** and pin CUDA **per container**. Because the
580 driver already supports everything up to CUDA 13, containers with CUDA 12.x run
cleanly and you avoid host "CUDA version hell." This also makes the host CUDA cleanup
less fragile and engines independently upgradable.
(`/srv/ai/docker` already exists for this.)

---

## 8. Intended workload split (user's plan — sound)

- **P100 (16GB)** → smaller models: **embeddings, TTS, STT, agent routing.**
  (Dirs already scaffolded: `/srv/ai/tts`, `/srv/ai/whisper`, `/srv/ai/models`.)
- **2× V100 (32GB each)** → **primary coding models**, either one model per card, or
  **tensor-parallel across both** for larger/long-context coding models.
  - ⚠️ TP across V100s has **no NVLink** → PCIe-bound; benchmark TP=2 vs single-card.
  - ⚠️ **Do not** tensor-parallel across mismatched cards (V100 32GB + P100 16GB).

---

## 9. Additional considerations / things to verify

1. **Cooling (critical, headless):** V100/P100 PCIe cards are **passively cooled**
   (designed for server chassis airflow). In this desktop board they need **forced
   air** (blower shrouds / ducted fans). Confirm a Linux **fan-control** solution and
   thermal monitoring (`nvtop` is installed) — Windows fan control does **not** carry
   over. Watch for thermal throttling under sustained load.
2. **Power/PSU:** 2×V100 + P100 ≈ 750W GPU + ~140W CPU. Need a **1000W+** PSU and the
   correct **8-pin EPS (CPU-style)** GPU power connectors these Teslas use (not standard
   PCIe). Verify connectors/adapters and PSU headroom.
3. **Persistence mode:** enable `nvidia-smi -pm 1` (via `nvidia-persistenced`) for
   stable headless behavior and faster context init.
4. **Power caps (optional):** `nvidia-smi -pl <watts>` to cap per-GPU power for
   thermal/PSU headroom.
5. **P2P / IOMMU:** for multi-GPU P2P over PCIe, check `nvidia-smi topo -m` and whether
   **ACS** needs disabling (kernel `pcie_acs_override` / IOMMU off) for P2P to engage.
6. **Secure Boot:** if enabled, DKMS modules need signing. Driver already loads, so
   likely **off** — confirm.
7. **API gateway:** to expose one OpenAI-style endpoint over multiple backends
   (P100 embeddings + V100 coding), consider **LiteLLM** as a router/proxy — fits the
   "agent routing" goal. Plan systemd services + auth + reverse proxy.
8. **Storage layout:** root FS is the 2TB NVMe (`/`, 1.1T free). Decide where large
   model weights live (`/srv/ai/models`) and back up configs.

---

## 10. Proposed task order

1. ✅ Verify driver healthy (done — 580-server, nvidia-smi OK).
2. ✅ **Clean-purge CUDA 13** toolkit (done 2026-06-30 via `scripts/purge-cuda13.sh`;
   64 pkgs removed, driver preserved, nvcc/`/usr/local/cuda*` gone, GPUs OK).
3. ✅ **Install CUDA 12.x** (done 2026-07-01 via `scripts/install-cuda129.sh`):
   CUDA 12.9.86 + cuDNN 9.23.2, `/usr/local/cuda`→`cuda-12.9`, driver preserved,
   nvcc targets sm_60+sm_70, end-to-end compile+run test passed on all 3 GPUs.
4. ✅ Build **llama.cpp** (done 2026-07-01 via `scripts/build-llama.sh`):
   clean CUDA build b9850/4f31eedb0, arch 60;70, LLAMA_CURL=ON, ccache on.
   Binaries in `/srv/ai/src/llama.cpp/build/bin/` (llama-server/cli/bench/embedding).
   Verified: detects all 3 GPUs; inference OK on V100 (~402 tg t/s) and P100
   (~233 tg t/s) with a 0.5B Q4_K_M test model.
5. ⏭️ Stand up **NVIDIA Container Toolkit** + Docker; reproduce llama.cpp in a container.
6. ⏭️ Evaluate **vLLM** on the V100s (containerized); benchmark vs llama.cpp.
7. ⏭️ Wire up **OpenAI-style API** + router (LiteLLM); systemd services.
8. ⏭️ Cooling/fan-control + thermal/power validation under sustained load.

---

## Quick reference — current state (2026-06-30)
```
OS:      Ubuntu 24.04.4 LTS, kernel 6.8.0-124-generic
Driver:  nvidia-driver-580-server 580.159.03  (KEEP)
CUDA:    12.9.86 installed ✅ (cuDNN 9.23.2); /usr/local/cuda -> cuda-12.9
nvcc:    /usr/local/cuda/bin/nvcc → 12.9 (targets sm_50..sm_121 incl sm_60/sm_70)
GPUs:    nvidia-smi order: 0=P100  1=V100  2=V100  (all OK on 580)
         CUDA runtime order (fastest-first): 0=V100 1=V100 2=P100
         → set CUDA_DEVICE_ORDER=PCI_BUS_ID to align with nvidia-smi
NVLink:  none (all PHB / PCIe gen3)
gcc:     13.3.0
llama.cpp clone: /srv/ai/src/llama.cpp
llama.cpp build: /srv/ai/src/llama.cpp/build/bin  (server/cli/bench/embedding)
docker:  not installed yet (for NVIDIA Container Toolkit step)
```

## Operator cheat-sheet — common commands

Everyday commands the human operator runs from the box (SSH). Native services are
systemd units and need `sudo`; the Docker app tier runs as `brad` (in the `docker`
group) so it needs **no** sudo. Automation/agents cannot sudo — these are for you.

### Restart native services (systemd)
```bash
sudo systemctl restart llama-swap                    # model router (YAML auto-reloads; see note)
sudo systemctl restart gpu-fan-control               # fan curves + power caps
sudo systemctl restart comfyui-open comfyui-secure   # both ComfyUI instances
sudo systemctl restart comfyui-mcp                   # ComfyUI image/video MCP tools
sudo systemctl restart server-status                 # status service / OWUI banner / fast keeper

systemctl status llama-swap --no-pager               # is it up?
journalctl -u llama-swap -e --no-pager               # recent logs
journalctl -u gpu-fan-control -f                     # follow live
```

### Restart the app tier (Docker Compose)
```bash
cd /srv/ai/docker
docker compose ps                                    # what's running
docker compose restart litellm                       # after editing litellm/config.yaml
docker compose restart open-webui
docker compose restart mcpo                          # after editing mcpo/config.json
docker compose up -d                                 # apply compose changes / start all
docker compose logs -f litellm                       # follow logs
```

### Common edits (what to change → how to apply)
| Change | Edit | Apply |
|--------|------|-------|
| Add/change a served model | `config/llama-swap.base.yaml` (model block **+** `matrix` set) **and** `docker/litellm/config.yaml` (matching `model_list` entry) | `scripts/llama-swap-mode.py set <current-mode>` to re-render the active `config/llama-swap.yaml` (llama-swap auto-reloads it); `docker compose restart litellm` |
| Always-on / preloaded model | `hooks.on_startup.preload` in `config/llama-swap.base.yaml` (or a mode's `preload:`) | re-render (`llama-swap-mode.py set <mode>`) then `sudo systemctl restart llama-swap` (boot preload only runs at process start) |
| Switch serving mode (daily / heavy-coding / agentic) | — | `scripts/llama-swap-mode.py set agentic` (no restart — renders `config/llama-swap.yaml`, `-watch-config` reloads, warms the mode's models). Or from a client via the [`llama-swap-mode` MCP](#llama-swap-mode-mcp-switch-serving-modes-from-a-client) (`set_mode`). `llama-swap-mode.py list` / `current` / `show <mode>` to inspect. |
| Add a serving mode | new `config/modes/<name>.yaml` overlay (`overrides` per-model `parallel`/`concurrencyLimit`/`ctx_size`, `preload`, `warm`) | `scripts/llama-swap-mode.py set <name>` |
| GPU power caps / fan curves | `scripts/gpu-fan-control.config.json` | `sudo systemctl restart gpu-fan-control` |
| New ComfyUI image/video MCP tool | drop a workflow JSON in `config/comfyui-mcp/workflows/` | `sudo systemctl restart comfyui-mcp` (new workflow files are **gitignored** by default — add to git only to publish) |
| Snapshot ComfyUI before a node-pack install | — | `scripts/comfyui-snapshot.sh` (captures venv pip freeze + custom_nodes git HEADs + a ComfyUI-Manager snapshot into `comfyui/backups/`); `scripts/comfyui-snapshot.sh --list` to list; restore via the Manager UI or `cm-cli.py restore-snapshot <STAMP>` |
| Model dropdown display names | Open WebUI → Admin → Settings → Models | stored in the OWUI DB, not the repo |
| Enable the OWUI status banner | `cp scripts/server-status.env.example scripts/server-status.env`, set `OWUI_API_KEY` | `sudo systemctl restart server-status` |

### Check state / warm the daily models
```bash
curl -s 127.0.0.1:9090/running | python3 -m json.tool     # loaded models
CUDA_DEVICE_ORDER=PCI_BUS_ID nvidia-smi                    # GPU util/VRAM/temp
curl -s 127.0.0.1:9095/status.json | python3 -m json.tool  # aggregated host+GPU+model status
curl -s 127.0.0.1:9095/history.json | python3 -m json.tool # time series behind the dashboard sparklines

# Warm the daily set after a restart (fast preloads itself; coding+chat load on first hit):
for m in coding chat fast; do
  curl -s 127.0.0.1:9090/v1/chat/completions -H 'content-type: application/json' \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" >/dev/null
done
```

Unload all router models (frees all VRAM). The `fast` keeper in `server-status` re-warms
`fast` within ~60 s, so stop that service first if you need the GPUs to stay idle. Note the
LLMs are not the only thing holding the cards: the two **ComfyUI** instances keep a resident
CUDA context on the V100s, which pins them to P0 (max clocks, ~36–37 W each) even with no
inference. To reach the **true cold-idle baseline** (e.g. to measure no-load power draw), stop
ComfyUI too:
```bash
sudo systemctl stop server-status                          # pause the fast keeper
curl -s -X POST 127.0.0.1:9090/api/models/unload -d '{}'   # unload every LLM
sudo systemctl stop comfyui-open comfyui-secure            # release the V100 CUDA contexts
CUDA_DEVICE_ORDER=PCI_BUS_ID nvidia-smi dmon -s put -c 15   # e.g. capture true idle draw
# restore:
sudo systemctl start comfyui-open comfyui-secure server-status
```
Leaving ComfyUI running is normal (image gen stays instant); just know the V100s won't
deep-idle until their CUDA context is gone.

### Quiet-hours deep-idle window
The `server-status` service can run an optional overnight window that drops the box to the
true cold-idle floor (~73 W GPU vs ~103 W warm; see the README Power-usage tables). On
entering the window it **unloads the daily models and stops both ComfyUI units** so the
V100s fall out of P0. LLM requests still auto-wake llama-swap on demand; when a client is
active during the window the service **restarts ComfyUI** so the box is fully ready. It
re-idles once **GPU SM utilization stays below `QUIET_ACTIVE_SM_PCT`% (default 5) for
`QUIET_ACTIVITY_GRACE` seconds** (default 600) — utilization is used rather than "a model
is loaded" because `coding`/`chat` have no ttl and would otherwise pin the box awake all
window. On re-idle the models are unloaded and ComfyUI stopped again; the next client
request reloads on demand. At window end the daily set (`coding`, `chat`) is re-warmed and
the `fast` keeper resumes.

Disabled by default. To enable:
```bash
# 1) grant the service scoped rights to stop/start ComfyUI (runs as brad):
sudo install -m 0440 -o root -g root \
  scripts/server-status-comfyui.sudoers /etc/sudoers.d/server-status-comfyui
sudo visudo -cf /etc/sudoers.d/server-status-comfyui        # syntax check

# 2) turn it on in server-status.env (defaults: 02:00–09:00 local):
echo 'QUIET_HOURS_ENABLED=true' >> scripts/server-status.env  # + optional QUIET_* overrides
echo 'QUIET_TZ=America/New_York' >> scripts/server-status.env # if the machine clock is UTC
sudo systemctl restart server-status
```
> **Note:** the window is evaluated in `QUIET_TZ` (or system local time if unset). This box's
> clock is `Etc/UTC`, so set `QUIET_TZ` to your wall-clock zone or the window will be hours off.
The current mode is reported as `power_mode` (`active` / `deep-idle` / `woken`) in
`curl -s 127.0.0.1:9095/status.json`. All `QUIET_*` knobs are documented in
`scripts/server-status.env.example`.

### Monitor GPU temps & fan speeds
The fan daemon drives the shroud fans off **HBM memory** temp (`mtemp`), which on the
V100s runs ~15-20 °C hotter than the core and throttles at ~85 °C.
```bash
# Easiest: follow the fan daemon's own log — it prints per-GPU temps and the fan
# duty %% it applies each tick:
journalctl -u gpu-fan-control -f

# GPU power + core (gtemp) + HBM (mtemp) + clocks, refreshing:
CUDA_DEVICE_ORDER=PCI_BUS_ID nvidia-smi dmon -s put

# Combined view: GPU temps + actual shroud fan RPM (passive Teslas report no fan to
# nvidia-smi, so RPM comes from lm-sensors)
#   shroud map: fan5 = V100 idx1 (bus03), fan4 = V100 idx2 (bus04), fan1 = P100 idx0
watch -n2 'CUDA_DEVICE_ORDER=PCI_BUS_ID nvidia-smi \
  --query-gpu=index,name,temperature.gpu,temperature.memory,power.draw --format=csv,noheader; \
  echo "-- shroud fans (fan5=V100-idx1, fan4=V100-idx2, fan1=P100-idx0) --"; \
  sensors | grep -E "fan[145]:"'
```

### Re-run benchmarks
`scripts/bench-models.sh` re-runs llama.cpp benchmarks for any subset of the served
models. The model registry (gguf path, GPU pinning, split mode) is read straight from
`config/llama-swap.yaml`, so it always matches what the router serves. No sudo needed.
```bash
scripts/bench-models.sh --list                 # show model names + GPU pinning
scripts/bench-models.sh                         # bench the daily set (coding chat fast)
scripts/bench-models.sh coding chat gemma-31b   # bench specific models by name
scripts/bench-models.sh --all                   # every model in the config
scripts/bench-models.sh --free coding           # unload llama-swap models first (avoid OOM)

# Tunables: -p prompt-toks  -n gen-toks  -r reps  -d "depths"  -o out-dir
scripts/bench-models.sh -p 512 -n 128 -d "0 8192 32768" coding
```
Results are written as Markdown to `models/bench-<timestamp>/results.md`. `llama-bench`
loads the model directly on its pinned GPU(s); if the router already has a model resident
there, pass `--free` (unloads all router models via the API, no sudo) or run when idle.

### Reboot into Windows (UEFI dual-boot)
This box is UEFI dual-boot: `Boot0000` = **Windows Boot Manager**, `Boot0004` =
**Ubuntu** (the default). Boot **once** into Windows, then it returns to Ubuntu on the
next restart automatically — `BootNext` is consumed after a single boot, so there is
nothing to undo:
```bash
efibootmgr | grep -i windows        # confirm the Windows entry number (Boot0000 here)
sudo efibootmgr --bootnext 0000     # one-shot: applies to the NEXT boot only
sudo systemctl reboot
```
To change the **permanent** boot order instead (e.g. Ubuntu first, Windows second):
```bash
sudo efibootmgr -o 0004,0000
```

### Reboot / shutdown the machine
```bash
sudo systemctl reboot
sudo systemctl poweroff
```

## llama.cpp usage notes (learned during bring-up)
- **Build:** `scripts/build-llama.sh` — clean rebuild, arch `60;70`, curl+ccache on.
- **GPU selection:** set `CUDA_DEVICE_ORDER=PCI_BUS_ID` so `CUDA_VISIBLE_DEVICES`
  indices match nvidia-smi (0=P100, 1/2=V100). Otherwise CUDA reorders V100s first.
- **`-hf` auto-download hits HTTP 401** (HF API now requires auth for the commit
  lookup). Workaround: download GGUFs directly via `curl` into `/srv/ai/models`
  and pass with `-m`, or configure an HF token later.
- **`llama-cli` waits at an interactive `>` prompt** in this headless/no-TTY env
  even with `-no-cnv` (looks like a hang, burns CPU). For scripted/non-interactive
  runs use **`llama-bench`** or **`llama-server`**; those exit cleanly.
- **Test model:** `/srv/ai/models/qwen2.5-0.5b-q4km.gguf` (0.5B, for smoke tests).

## Benchmarks → see [benchmarking.md](benchmarking.md)

All model performance benchmarks (single-stream `llama-bench` runs, the MoE and
coding-model comparisons, the tensor-parallel/multi-GPU reality check, the
context-window and `--ubatch-size` tuning, the Gemma-4 numbers) and the concurrency
/ `--parallel` throughput sweep now live in **[docs/benchmarking.md](benchmarking.md)**.

## GPU fan control (shroud fans) — runbook (2026-07-01)
Passive Tesla cards throttle under load (seen via `nvidia-smi dmon`). Shroud fans
are on the board's **4-pin PWM headers** (Nuvoton nct6775). Control = GPU temp
(nvidia-smi) → PWM. See ADR-0009. Scripts in `/srv/ai/scripts/`:

1. **`sudo ./setup-fan-sensors.sh`** — installs lm-sensors, loads `nct6775`
   (adds `acpi_enforce_resources=lax` to GRUB if the chip won't bind → **reboot**,
   then re-run). Prints the exposed `pwmN` channels + fan RPMs.
2. **`sudo ./identify-fan.sh`** — pulses each `pwmN` low→high so you can see which
   channel spins each card's shroud fan. Restores auto on exit.
3. Edit **`gpu-fan-control.config.json`** — set each zone's `pwm` to the channel
   found in step 2 (GPU idx: 0=P100, 1=V100, 2=V100). Curve = [tempC, duty%],
   min 35%, 100% by 80 °C.
4. **`sudo ./install-fan-service.sh`** — installs+starts `gpu-fan-control.service`.
   Logs: `journalctl -u gpu-fan-control -f`.

Daemon = `gpu-fan-control.py` (stdlib only). Fail-safe: forces fans to **100%** on
any error/`nvidia-smi` failure; hands back to BIOS auto on clean stop.

### Fan wiring + curves (confirmed from Windows FanControl, 2026-07-01)
Fans: 40x28mm high-static-pressure. V100 fans 15k rpm max; P100 fan 6k rpm max
(quieter). Ambient 72-76 °F. CPU on Cooler Master block via CPU_FAN; case has
adequate airflow.

| GPU (nvidia-smi) | PCI bus | Card   | pwm channel | Idle→Load | Duty     |
|------------------|---------|--------|-------------|-----------|----------|
| GPU1             | 03      | V100   | **pwm5**    | 39→70 °C  | 35→100 % |
| GPU2             | 04      | V100   | **pwm4**    | 39→70 °C  | 35→100 % |
| GPU0             | 01      | P100   | pwm1        | 38→70 °C  | 70→100 % |

Curves are linear between idle and load temp (hold min below idle, 100% at/above
70 °C) — encoded in `gpu-fan-control.config.json`.

**pwm→GPU mapping was verified physically (2026-07-01) with
`verify-gpu-fan-mapping.sh`**, a forced-fan idle cross-test (drive one V100 fan to
MAX, the other to a floor, see which GPU cools). Result: **pwm5 cools GPU1, pwm4
cools GPU2** — the *opposite* of the initial `identify-fan.sh` guess. Lesson: watching
a fan's RPM track a GPU's temp under the running daemon is **circular** — it only
proves the daemon drives that pwm from that GPU, not which card the fan physically
sits on. Always confirm with the forced-fan cross-test. (`identify-fan.sh` is still
useful for pwm↔tach discovery and the P100/case/side-fan classification.)

### Fan control drives off HBM MEMORY temp (2026-07-01)
The daemon controls each V100 zone on **max(core, memory)** temp, not core alone.
Reason: the V100's HBM2 `temperature.memory` runs ~15-20 °C HOTTER than the core and
is the throttle limiter (~85 °C). Controlling on core (64 °C ≈ 87% fan) let the HBM
cook. Now the fan pins 100% as memory climbs. (P100 reports `temperature.memory`=N/A
→ uses core.) Log format: `c<core>/m<mem>C->duty%`.

**Physical cooling ceiling (important):** even at 100% fan (15k rpm) the 40 mm shroud
cannot dissipate a full 245 W memory-bound load — under `llama-bench -p 2048` (a
worst-case, memory-bandwidth-saturating prefill) the HBM plateaus at 85 °C and the
card SOFT-throttles itself to ~180 W / ~1130 MHz to hold that temp. This is safe
(within HBM2 spec) and expected for a passive Tesla + 40 mm fan. Normal inference/
serving (token-gen dominated, far lower sustained mem BW) should not reach this.
To ELIMINATE throttle under max load, cap power/clocks:
  `sudo nvidia-smi -i 1 -pl 200`   (range 100-250 W; card self-limits ~180 W at 85 °C)
  or lock the graphics clock: `sudo nvidia-smi -i 1 -lgc <MHz>` (mem clock is fixed 877).
Enable `nvidia-smi -pm 1` (persistence) so caps survive. Tune per-card if desired.

### Power-cap sweep results + automatic caps at boot (2026-07-01)
`power-cap-sweep.sh` sweeps caps and benches prefill+decode, recording peak temps and
throughput. Results (Qwen3.6-27B on V100s, Qwen3.5-9B Q8_0 on P100):

| GPU | 250W | 200W | 175W | 150W | chosen |
|-----|------|------|------|------|--------|
| V100 GPU1 (idx1) | 85 °C | 85 °C | **84 °C** | 78 °C | **175W** |
| V100 GPU2 (idx2) | 86 °C | 85 °C | **83 °C** | 77 °C | **175W** |
| P100 (idx0) core | 66 °C* | 75 °C | 73 °C | 70 °C | **200W** |

*P100 250W's low reading is a cold-start artifact; it settles ~70-75 °C under sustained
load regardless of cap — **never throttles** (15 °C headroom). Both V100s peg 85 °C and
soft-throttle at ≥200W; **175W holds ~83-84 °C at ~91% decode throughput**. Decode is
HBM-bandwidth-bound (mem clock locked 877 MHz), so capping power costs mostly prefill,
little token-gen. P100 200W is a longevity/noise trim at ~0% throughput cost.

**These caps are now applied automatically at boot** by the `gpu-fan-control` service via
a top-level `power_limits` object in `gpu-fan-control.config.json`:
```json
"power_limits": { "0": 200, "1": 175, "2": 175 }   // gpu_index: watts
```
`gpu-fan-control.py:reconcile_power_limits()` runs `nvidia-smi -i N -pm 1 -pl W` for each
entry (logged as `power cap GPUN 250W -> 175W`). A capping failure is **non-fatal** — the
fan daemon keeps running (airflow is the safety-critical function). To change caps, edit the
config and re-run `sudo install-fan-service.sh` (validates 50-400 W range). Board limits:
V100 100-250 W, P100 125-250 W.

**Boot/recovery self-heal (2026-07-14).** After a wall-power loss, a V100 (bus04/idx2)
"fell off the bus" (`NVRM: ... has fallen off the bus`; visible in `lspci` but not
`nvidia-smi`). A PCIe re-probe recovered it, but it came back at its default **250 W**
because the old `apply_power_limits()` ran only **once** at startup — when idx2 was still
absent — and never re-checked. The daemon now hardens both cold and warm starts so no
manual capping is needed:
- **`wait_for_gpus()`** — bounded startup wait until `nvidia-smi` reports
  `expected_gpu_count` (3) GPUs before capping, guarding the boot enumeration race.
- **`reconcile_power_limits()`** — idempotent, drift-only (queries live `power.limit`,
  fixes only what's wrong/missing); called at startup **and every `power_recheck_sec`
  (30 s)** in the main loop, so a GPU missing at boot or returning after a re-probe gets
  capped automatically. Fans already self-heal (loop re-queries temps each cycle).
- Config keys: `expected_gpu_count`, `gpu_wait_timeout_sec`, `power_recheck_sec`.

Recovering a fallen-off-the-bus card without a full reboot (needs root):
```bash
echo 1 | sudo tee /sys/bus/pci/devices/0000:04:00.0/remove
sudo sh -c 'echo 1 > /sys/bus/pci/rescan'
nvidia-smi -L                              # card should reappear
sudo systemctl restart gpu-fan-control     # (or just wait ≤30 s for auto-reconcile)
```
A **full cold power cycle** (PSU off ~30 s) clears the latched fault more reliably than a
warm reboot, which keeps standby power on the card. If it keeps recurring, reseat the PCIe
power cables and the card in its slot.

## Phase 2 — llama-swap model router (2026-07-02)

Native on-demand model router in front of `llama-server`, OpenAI-compatible. Binary
`/srv/ai/bin/llama-swap` (v234); config `/srv/ai/config/llama-swap.yaml`; systemd unit
`llama-swap.service` (binds `127.0.0.1:9090`, runs as `brad`, `-watch-config`).
Install/update: `sudo /srv/ai/scripts/install-llama-swap-service.sh`.

**Models / GPU map** (`CUDA_DEVICE_ORDER=PCI_BUS_ID`; idx0=P100, idx1/2=V100):

| model    | file                               | GPU(s)      | ctx   | VRAM   |
|----------|------------------------------------|-------------|-------|--------|
| `coding` | Qwen3.6-27B **Q6_K**               | idx1        | 204800 | ~29.8 GB (q8_0 KV) |
| `chat`   | Qwen3.6-35B-A3B **UD-Q6_K**        | idx2        | 16384 | ~28 GB (q8_0 KV) |
| `big`    | Qwen3.6-27B **BF16** (split)       | idx1+idx2   | 16384 | ~51 GB (25+26), ttl 300s |
| `fast`   | **Gemma-4-26B-A4B** MoE QAT UD-Q4_K_XL | idx0 (P100) | 32768 | ~15.3 GB, always-on, `--reasoning-budget 0`, ub1024 (SWAPPED 2026-07-22 from Gemma-4-12B) |
| `fast-12b` | **Gemma-4-12B** QAT UD-Q4_K_XL   | idx0 (P100) | 131072 | ~10.8 GB dense fallback for max ctx/headroom, ttl 600s, shares idx0 w/ `fast` |
| `gemma-31b` | **Gemma-4-31B** QAT UD-Q4_K_XL  | idx1        | 131072 | ~26 GB (q8_0 KV), ttl 600s (evicts coding), ub2048 |
| `gemma-26b` | **Gemma-4-26B-A4B** MoE QAT     | idx2        | 131072 | ~18 GB, ttl 600s (evicts chat), ub2048 |

**Open WebUI display names.** The model-picker dropdown shows a friendly label per
model so the profile names aren't mixed up, set via **Admin Panel → Settings → Models →**
(edit each model's **Name**). This overrides *only* the display label — the API id stays
the short name (`chat`, `coding`, …) so the plan-build MCP tool, llama-swap routing, and all
`model=…` calls are unaffected. It persists in Open WebUI's DB (not in this repo). Mapping:

| API id (unchanged) | Open WebUI display name |
|--------------------|-------------------------|
| `coding`           | `coding (Qwen3.6-27B)` |
| `chat`             | `chat (Qwen3.6-35B-A3B MoE)` |
| `big`              | `big (Qwen3.6-27B BF16)` |
| `coder-next`       | `coder-next (Qwen3-Coder-Next 80B-A3B)` |
| `fast`             | `fast (Gemma-4-26B-A4B MoE)` |
| `fast-12b`         | `fast-12b (Gemma-4-12B dense)` |

(The `plan-build` MCP tool carries the same labels in its output bylines/param hints via its
`MODEL_LABELS` map — keep the two in sync if a model is swapped.)

**Routing = matrix (3 cards).** `f`(fast, P100) is in every set so it's never evicted and runs
CONCURRENTLY with the V100 models. V100 sets: `qq: c & h & f` (daily), `qg: c & y & f`,
`gq: x & h & f`, `gg: x & y & f` (any Qwen/Gemma pairing across idx1/idx2), `max: b & f`
(big splits both V100s). Verified 2026-07-02: coding(idx1)+chat(idx2)+fast(P100) all
co-resident; after the 2026-07-22 swap `fast` (MoE) uses ~15.3 GB on the P100 (was ~7.7 GB as the 12B). `fast` answers immediately (Gemma reasoning disabled).

**Gemma-4 note:** Gemma-4 is a **hybrid reasoning** model (thoughts land in `reasoning_content`).
`fast` sets `--reasoning-budget 0` to skip thinking for snappy chat; the comparison models
`gemma-31b`/`gemma-26b` keep reasoning on. All use QAT UD-Q4_K_XL (unsloth) — 4-bit quality
close to full precision. Our llama.cpp build (9850, `LLM_ARCH_GEMMA4`) supports them natively.

**Behavioural notes:** these are reasoning models — final answer is in `content`, chain-of-thought
in `reasoning_content`; budget `max_tokens` generously (≥512) or `content` returns empty with
`finish_reason: length`. `--jinja` is on (tool-calling chat template). API is default-allow on
localhost; auth is enforced at the LiteLLM gateway (next phase). Endpoints: `/v1/models`,
`/v1/chat/completions`, `/running`, `POST /api/models/unload`, web UI at `:9090`.

## Phase 2b — LiteLLM gateway (2026-07-02)

OpenAI/Anthropic-compatible gateway (container, hybrid ADR-0006) in front of the native
llama-swap router. Compose stack at `/srv/ai/docker/`; image `ghcr.io/berriai/litellm:v1.90.0`.
Start/stop: `cd /srv/ai/docker && docker compose up -d` / `down`. Auto-starts on boot
(`restart: unless-stopped` + docker enabled).

**Networking:** `network_mode: host` — reaches llama-swap on `127.0.0.1:9090` and exposes the
gateway on the host at `:4000`. (Bridge networking can't reach a localhost-bound host service,
hence host mode.) Config `docker/litellm/config.yaml` maps model names `coding`/`chat`/`big` to
`openai/<id>` at `api_base http://127.0.0.1:9090/v1`.

**Auth:** `master_key` from `LITELLM_MASTER_KEY` in `docker/.env` (gitignored; template in
`.env.example`). Clients send it as their OpenAI API key (`Authorization: Bearer sk-...`).
Requests without a key get 401. Verified 2026-07-02: `/v1/models` lists all three; full path
client→LiteLLM(:4000)→llama-swap(:9090)→llama-server returns correct output.

**Client setup** (Copilot CLI / VS Code / any OpenAI client):
- Base URL: `http://<server-or-tailscale-ip>:4000/v1`
- API key: the `LITELLM_MASTER_KEY`
- Models: `coding`, `chat`, `big`
Later phases add a reverse proxy + Tailscale-only binding; for now access over the trusted
network. `drop_params: true` and `request_timeout: 600` accommodate llama-server quirks and
cold model loads (~30-70s).

**Pre-call sanitizer (`docker/litellm/custom_hooks.py`):** llama.cpp's OpenAI endpoint
rejects any assistant message with neither a `content` key nor `tool_calls`
(`400 - Assistant message must contain either 'content' or 'tool_calls'!`). Agentic clients
(e.g. Copilot CLI BYOK) can persist a content-less assistant turn into their transcript when a
model errors mid-session — every later request then replays it and 400s the whole session. The
`callbacks: custom_hooks.proxy_handler_instance` hook gives such messages an empty-string
`content` (accepted by llama-server), so one bad turn no longer bricks a session and no context
is lost. Mounted read-only into the container; restart `litellm` after editing.

## GitHub Copilot CLI via BYOK (2026-07-02)

Copilot CLI supports OpenAI-compatible endpoints (BYOK). It points at the LiteLLM gateway.
Requirements (both verified through the full stack 2026-07-02): **tool calling** (Qwen3.6 +
`--jinja` → `finish_reason: tool_calls`) and **streaming** (SSE). Docs recommend a ≥128k context
window for best results; all of the models below meet that.

Env vars (see `scripts/copilot-byok.sh`, which sources the key from `docker/.env`):

    export COPILOT_PROVIDER_BASE_URL=http://<host>:4000/v1   # e.g. Tailscale <tailscale-ip>
    export COPILOT_PROVIDER_TYPE=openai
    export COPILOT_PROVIDER_API_KEY=$LITELLM_MASTER_KEY
    export COPILOT_MODEL=coding        # or chat / big / coder-next / fast
    copilot

On the server just run `/srv/ai/scripts/copilot-byok.sh`. If the endpoint 404s, try the base URL
without the trailing `/v1`.

### Per-model token budgets (`COPILOT_PROVIDER_MAX_*`)

`copilot-byok.sh` auto-sets the prompt/output token budgets per model. The governing rule:
llama.cpp's `--ctx-size` KV cache is **shared** between prompt and generation, so

> **`MAX_PROMPT_TOKENS` + `MAX_OUTPUT_TOKENS` ≤ ctx-size**, keeping ~15–20% headroom

for tokenizer drift (Copilot's count ≠ the model's) and the flash-attn compute buffer.
**Reasoning** models get a bigger output budget because their hidden thinking phase spends
output tokens; **non-thinking** models don't, so their output cap can be smaller.

| `COPILOT_MODEL` | ctx-size | reasoning | `MAX_PROMPT_TOKENS` | `MAX_OUTPUT_TOKENS` | prompt+output |
|-----------------|---------:|-----------|--------------------:|--------------------:|--------------:|
| `coding`     | 204800 | yes | 131072 | 32768 | 163840 (~40k spare) |
| `chat`       | 131072 | yes |  81920 | 24576 | 106496 (~24k spare) |
| `big`        | 262144 | yes | 163840 | 32768 | 196608 (~65k spare) |
| `coder-next` | 262144 (131072/slot, `--parallel 2`) | **no** (agentic) | 98304 | 32768 | 131072 (fits 1 slot) |
| `fast`       | 32768 | **no** |  24576 |  8192 | 32768 (fits) |
| `fast-12b`   | 131072 | **no** |  98304 |  8192 | 106496 (~24k spare) |
| *(other)*    | — | — | 32768 | 8192 | conservative fallback |

Notes:
- These are **ceilings for reliability, not "always use the max"** — long prompts prefill
  slowly on a single V100 (~790 t/s ≈ 2–3 min for a 128k prefill) and quality degrades well
  before the ctx limit. For snappier `coding` turns, drop the prompt cap (e.g. `65536`).
- **`coder-next`** is the strongest agentic BYOK coder here: non-thinking (no wasted
  reasoning tokens), 256k native ctx with cheap KV, ~77 t/s decode — but it splits across
  **both** V100s and **preempts `coding`+`chat`** (like `big`), so it evicts the daily set.
  It runs `--parallel 2` (so main agent + subagent don't thrash one KV slot), which makes each
  slot **131072** ctx — hence the 98304+32768 budget fits a single slot. A prompt above ~131k
  would exceed a slot and error/truncate.
- Any value exported in the environment overrides the per-model default, e.g.
  `COPILOT_MODEL=coding COPILOT_PROVIDER_MAX_PROMPT_TOKENS=65536 copilot-byok.sh`.

### Subagent model routing (GPU-tiered)

The goal: run each Copilot subagent on a *different* local model — and therefore a
different GPU — so the driver keeps reasoning while subagents work **in parallel**:

| GPU | Model | Role |
| --- | --- | --- |
| V100 idx1 | `coding` (default `COPILOT_MODEL`) | primary driver |
| V100 idx2 | `chat` | task/general subagent |
| P100 idx0 | `fast` (Gemma-4-26B-A4B MoE) | explore/search subagent (always warm) |

Because `fast`/`chat` live on separate cards from the driver, subagents run with no
contention, no eviction, and (for `fast`) zero cold-start (the status service's keeper
keeps `fast` resident). All three route through LiteLLM `:4000` → llama-swap → the
right GPU, so no server-side change is needed — only the client picks the per-agent model.

**What works where (verified live 2026-07-18, CLI 1.0.71 / `@github/copilot-sdk@1.0.7`):**

1. **Env-var BYOK (`copilot-byok.sh`) — single model only.** `copilot help providers`
   confirms the env-var path (`COPILOT_PROVIDER_BASE_URL` + `COPILOT_MODEL`) registers
   exactly one BYOK model, so the `/agents` picker can't offer `chat`/`fast` to subagents.
   `SEARCH_SUBAGENT_MODEL=fast` is set in the launcher but is **inert**: the search
   subagent is gated by the account flag `copilot_swe_agent_cli_search_subagent`, whose
   availability is **`off`** (server-only) — not reachable via env,
   `COPILOT_CLI_ENABLED_FEATURE_FLAGS`, or `/experimental`. Proven: a delegated explore
   left `fast` at 0 tokens. Kept in the script so it auto-activates if GitHub flips the flag.

2. **Copilot SDK host — full GPU tiering, PROVEN.** A small `@github/copilot-sdk` program
   registers all local models via `onListModels` and pins per-agent models via
   `customAgents[].model`, all pointed at the singular LiteLLM provider (no GitHub auth
   needed). In a live PoC, an `explorer` subagent pinned to `chat` ran **81,778 tokens on
   V100 idx2** (idx2 pegged 70–94%) while the driver `coding` stayed on idx1 — event trace
   `subagent.started explorer chat` → `tool.execution_* chat` → `subagent.completed`. This
   is the sanctioned path for GPU-distributed local subagents; it's a scripted/headless
   surface, not the stock TUI. Minimal recipe:

   ```js
   import { CopilotClient, approveAll } from "@github/copilot-sdk";
   const client = new CopilotClient({ onListModels: () => localModels /* coding, chat, fast */ });
   await client.start();
   const session = await client.createSession({
     model: "coding",
     provider: { type: "openai", baseUrl: "http://127.0.0.1:4000/v1", apiKey: LITELLM_MASTER_KEY },
     onPermissionRequest: approveAll,
     customAgents: [{ name: "explorer", description: "read-only code explorer",
                      tools: ["read_file","grep_search","file_search","shell"], model: "chat" }],
   });
   ```
   (The SDK also exposes an experimental multi-provider registry — `providers[]` +
   `models[]` with per-model `wireModel` — for mixing CAPI + several BYOK providers.)

3. **GitHub Copilot desktop app — WORKS for multi-model reviews (verified 2026-07-18).**
   Configure `coding`/`chat`/`fast` as separate BYOK models in the app, then ask for a
   multi-model review (e.g. *"review the codebase using the `coding` model and the `chat`
   model"*). The app **does** fan out into parallel per-model review subagents — observed
   live: a `coding` reviewer ran on V100 idx1 and a `chat` reviewer on V100 idx2
   concurrently, then the driver compared both. (This contradicts an earlier assumption that
   the `copilot_cli_subagent_parallelism_prompts` flag blocks it — the app fanned out anyway.)

   **Two gotchas that will break it:**

   - **Exact lowercase model ids.** LiteLLM is case-sensitive: the driver once passed
     `model=Chat` and got `400 Invalid model name` (call `/v1/models` for the canonical ids:
     `coding`, `chat`, `fast`, …). Name the BYOK models exactly as registered — all lowercase.
   - **Do NOT expose the `plan-build` MCP to a review/parallel session.** Its tools are
     **serial** (blocking `_chat` calls) and the heavy ones swap `big`/`coder-next` onto
     **both V100s**, evicting the `coding`/`chat` models the review subagents are running on
     (the P100-only guard is bypassed by the HTTP service's `PLAN_BUILD_CALLER_GPU=p100`
     fallback). Symptom seen live: one reviewer stalled at ~2k generated tokens while its
     model was evicted mid-flight. Disable the plan-build MCP for review sessions
     (`COPILOT_PLAN_BUILD_MCP=0` for the CLI launcher, or turn it off in the app's MCP/tools
     settings) — it's a plan→build code-gen tool, not a review tool.

   (`RUBBER_DUCK_AGENT` is `on` and auto-invokes a second-opinion reviewer, but
   `rubberDuckSelectModel` picks a **cross-family** reviewer — all-local BYOK models are one
   family, so it likely won't pair. Worth a quick `/experimental` test but not relied upon.)

**Bottom line:** per-subagent local models are **not** achievable through `copilot-byok.sh`
env-var BYOK alone (single model). Use either the **SDK host** (proven, deterministic
per-agent pinning) or the **desktop app's multi-model BYOK** (proven for parallel reviews —
mind the two gotchas above). For the desktop app, see the paste-ready global-instructions
mapping and multi-session parallelism recipe in **`docs/copilot-app-instructions.md`**.

### plan-build MCP over HTTP (for Copilot BYOK)

The in-house **plan-build** planner→coder pipeline (`docker/mcpo/plan_build_mcp.py`, see the
mcpo section below) is also exposed to the Copilot CLI as a **remote HTTP MCP server**, so a
client — including a Mac with no Python/`uv` — can use its tools with zero local dependencies.

- **Server:** native systemd service `scripts/plan-build-mcp.service` runs the *same* script
  with `PLAN_BUILD_TRANSPORT=streamable-http` on **`0.0.0.0:9100`** (endpoint `/mcp`), reusing
  the `comfyui-mcp` venv (it already has `mcp[cli]`). **No auth** (like `comfyui-mcp`) — LAN/
  Tailscale only. It reads `LITELLM_MASTER_KEY` from `docker/.env` via `EnvironmentFile`. The
  stdio path mcpo uses for Open WebUI is unchanged (transport defaults to stdio).
  Install (needs sudo): `sudo /srv/ai/scripts/install-plan-build-mcp-service.sh`
- **Client registration:** `copilot-byok.sh` auto-registers it (idempotent) via
  `copilot mcp add --transport http plan-build <url>`, deriving the URL from
  `COPILOT_PROVIDER_BASE_URL` (same host, port 9100, `/mcp`). Override with
  `PLAN_BUILD_MCP_URL`/`PLAN_BUILD_MCP_PORT`, or opt out with `COPILOT_PLAN_BUILD_MCP=0`.
  Manual: `copilot mcp add --transport http plan-build http://<host-or-tailscale>:9100/mcp`.
- **`caller_gpu` over a shared endpoint:** the guard (see mcpo section) normally requires each
  call to report `caller_gpu` so a driver model can't evict itself. A shared HTTP service can't
  know each session's model, so the unit sets a service-wide default `PLAN_BUILD_CALLER_GPU=p100`
  — i.e. it assumes clients drive the P100 `fast` chat model, letting `big`/`coder-next` swap
  onto the V100s without evicting the caller. **If a BYOK session instead drives a V100 model**
  (`coding`/`chat`/`big`/`coder-next`), use only the `fast_*` tools (they never evict the daily
  V100 set). Override the default with `PLAN_BUILD_CALLER_GPU` on the service.

### llama-swap-mode MCP (switch serving modes from a client)

The serving-mode switcher (`scripts/llama-swap-mode.py`, see ADR-0015) is also
exposed as an **HTTP MCP server** so a client — Open WebUI (via mcpo) or a Copilot
BYOK/CLI session — can list, inspect, and switch the active llama-swap mode
(`daily` / `heavy-coding` / …) without shelling into the box. Switching only
rewrites a few `--parallel` / `concurrencyLimit` knobs + the preload list in the
generated `config/llama-swap.yaml`; llama-swap's `-watch-config` reloads it, so
**no restart or sudo** is needed.

- **Tools** (source `docker/mcpo/llama_swap_mode_mcp.py`):
  - `list_modes` — available modes + which one is active.
  - `current_mode` — active mode + effective per-model config (parallel / ctx / ctx-per-slot / concurrencyLimit / gpus).
  - `show_mode <mode>` — preview the config a mode *would* produce (no change).
  - `set_mode <mode>` — render + activate a mode and warm its models (a large (re)load can take a few minutes; `MODE_SWITCH_TIMEOUT` default 900 s).
- **Server:** native systemd service `scripts/llama-swap-mode-mcp.service` runs the
  script with `MODE_MCP_TRANSPORT=streamable-http` on **`0.0.0.0:9120`** (endpoint
  `/mcp`), reusing the `comfyui-mcp` venv (it has `mcp`). It runs on the **host**
  (not in the mcpo container) because it edits `config/llama-swap.yaml` and calls
  llama-swap on `127.0.0.1:9090`. It invokes the switcher with the system Python
  (`MODE_SWITCH_PY=/usr/bin/python3`, which has PyYAML). **No auth** (like
  `comfyui-mcp` / `plan-build`) — LAN/Tailscale only. Ordered `After=llama-swap.service`
  so boot-time warms succeed. Install (needs sudo):
  `sudo /srv/ai/scripts/install-llama-swap-mode-mcp-service.sh`
- **Open WebUI** talks to it through **mcpo (OpenAPI), not the raw MCP port.** It is
  registered in `docker/mcpo/config.json` as a `streamable-http` server pointing at
  `http://host.docker.internal:9120/mcp`; mcpo re-exposes it as an OpenAPI tool.
  Add it in **Settings → Integrations → External Tool Servers → Add**:
  - **URL:** `http://<host-or-tailscale>:8000/llama-swap-mode` — the **mcpo proxy on
    `:8000`**, *not* `:9120/mcp` (that's the raw MCP endpoint, which OWUI can't speak).
    Use the same address your browser reaches Open WebUI by (LAN IP vs Tailscale).
  - **Auth:** API key = `MCPO_API_KEY` from `docker/.env`.

  **Two gotchas:** (1) the host `:9120` service must be **running before mcpo (re)mounts
  the route** — mcpo only connects to an HTTP MCP backend at load and gives up if it's
  down (logs `Failed to create server 'llama-swap-mode'`), so after installing the
  service run `cd /srv/ai/docker && docker compose restart mcpo`. Verify with
  `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/llama-swap-mode/docs`
  (expect `200`). (2) `--hot-reload` picks up config.json *edits*, but reviving a
  previously-failed HTTP backend needs the restart.
- **Copilot CLI / BYOK:** register the **raw MCP** endpoint (`:9120/mcp`) directly — it
  is **not** auto-added by `copilot-byok.sh`:
  `copilot mcp add --transport http llama-swap-mode http://<host-or-tailscale>:9120/mcp`
  (use the LAN IP or Tailscale name the client reaches the server by). Any other
  MCP-over-HTTP client connects to the same `:9120/mcp` endpoint.

### Model context-window / ubatch tuning → see [benchmarking.md](benchmarking.md)

The `coding` context-window sweep, the `--ubatch-size` prefill tuning, and the
Gemma-4 throughput + context/VRAM tables that set the current per-model
`--ctx-size` / `-ub` / KV-quant args now live in
**[docs/benchmarking.md](benchmarking.md)** (Single-stream engine benchmarks).

## Phase 3 — Open WebUI + SearXNG + mcpo (2026-07-02)

App-tier containers (compose `/srv/ai/docker/`), all pointing at the LiteLLM gateway.
Start/stop: `cd /srv/ai/docker && docker compose up -d` / `down`. Reboot-safe
(`restart: unless-stopped`). Shared bridge network `ai`; LiteLLM stays host-networked and
is reached from containers via `host.docker.internal:4000`.

| Service     | Image                              | Access                     | Purpose |
|-------------|------------------------------------|----------------------------|---------|
| open-webui  | ghcr.io/open-webui/open-webui:v0.10.2 | `http://<host>:3000`    | Family chat UI + accounts |
| searxng     | searxng/searxng:latest             | `127.0.0.1:8888` (debug)   | Private web search (JSON) |
| mcpo        | ghcr.io/open-webui/mcpo:main       | `0.0.0.0:8000` / `mcpo:8000` | MCP→OpenAPI proxy (ADR-0011) |

**Open WebUI:** backend `OPENAI_API_BASE_URL=http://host.docker.internal:4000/v1` with the
LiteLLM master key; sees `coding`/`chat`/`big`. First browser signup becomes **admin**; add
family accounts under Admin → Users. Web search is pre-wired (`ENABLE_WEB_SEARCH=true`,
`WEB_SEARCH_ENGINE=searxng`); enable it per-chat with the web/globe toggle. Data persists in
the `open-webui-data` volume.

**SearXNG:** `search/formats` includes `json` (required by Open WebUI). Secret injected from
`SEARXNG_SECRET` (settings.yml keeps the literal `ultrasecretkey` placeholder). Verified:
`/search?q=...&format=json` returns results.

**mcpo (MCP hosting):** inventory = `docker/mcpo/config.json` (`mcpServers`, Claude-Desktop
format), tracked in git, `--hot-reload`. Each server → authed OpenAPI route
`http://mcpo:8000/<name>` (docs at `/<name>/docs`), bearer `MCPO_API_KEY`. Ships `uvx`
(Python servers work; Node/`npx` needs a node-enabled image). Registered servers:
`time`, `fetch` (URL→markdown), `git` (inspect the ai-server repo) — all `uvx` — plus
`plan-build`, an in-house planner→coder pipeline (see below). Verified live, e.g.
`POST /time/get_current_time {"timezone":"America/New_York"}` → datetime;
`POST /fetch/fetch {"url":"https://example.com"}` → page markdown;
`POST /git/git_log {"repo_path":"/repos/ai-server"}` → commit history.
Published on `0.0.0.0:8000` (API-key protected) because Open WebUI fetches/validates tool
specs and the browser may too — the URL must be reachable from the client, not just the
open-webui backend.

_git server notes:_ `/srv/ai` is bind-mounted **read-only** at `/repos/ai-server` (compose
mcpo `volumes`). Read ops (log/diff/status/show) work; `git_commit`/`git_add` fail by design
(`Read-only file system`) — no LLM-driven mutation of the real repo. The container user ≠
host owner (uid 1000), so the git server sets `safe.directory` via `GIT_CONFIG_*` env in
config.json (avoids git's "dubious ownership" error without writing host files). **Adding a
repo/volume mount requires `docker compose up -d --force-recreate mcpo`** — hot-reload only
picks up config.json edits, not new env/volumes.

_plan-build server (in-house planner→coder pipeline):_ source
`docker/mcpo/plan_build_mcp.py` (mounted at `/config`), launched with
`uv run --with mcp` (uv builds an ephemeral venv with the `mcp` package on first start;
`UV_CACHE_DIR=/tmp/uv-cache` since `/config` is read-only). It exposes three tools that call
the **LiteLLM gateway** and do the heavy lifting on the V100s while the `fast` chat model
(P100, never evicted) invokes them and relays output:
`make_plan` (a reasoning model — default `big` — writes a detailed plan),
`plan_and_build` (plan with `big`, then implement with `coder-next`), and
`fast_plan_and_build` (the interactive path: plan with `chat` + implement with `coding` — the
two daily V100 models that stay co-resident, so **no GPU swap**; may also be called from those
V100 models, not just `fast`), `fast_make_plan` / `fast_implement_spec` (the plan-only /
implement-only halves of that fast path, using `chat` / `coding` respectively),
`implement_spec` (implement a given spec directly with `coder-next`, no planning), and
`reset_models` (the "done" call: warm the default V100 models — `coding`+`chat`,
`PLAN_BUILD_DEFAULT_MODELS` — back onto the cards, evicting any `big`/`coder-next` left
resident). Planner
(`big`/`chat`/`fast`) and coder are overridable per call. Because `big` and `coder-next`
both need the two V100s, a `plan_and_build` call swaps `big` in (evicting coding+chat), then
`coder-next` in (evicting `big`); `fast` stays resident so the chat keeps responding. **So it
must only be called from a P100-exclusive model** — a V100 caller (coding/chat/big) would
evict *itself* mid-call and break the conversation. The tool can't see its caller through the
MCP protocol, so it takes a **`caller_gpu`** arg the model reports (via its system prompt) and
refuses anything not P100-exclusive (allowlist `PLAN_BUILD_SAFE_GPUS`, default `p100`). Enforce
it two ways: (1) in Open WebUI, enable this tool **only on the `fast` model**; (2) add to the
`fast` model's system prompt: _"When calling any plan-build tool, always pass
`caller_gpu="p100"`."_ **This
needs container networking + the gateway key:** the mcpo service adds
`extra_hosts: host.docker.internal:host-gateway` (LiteLLM runs `network_mode: host` on
`:4000`) and `env_file: ./.env`; mcpo passes `{**os.environ, **cfg.env}` to the stdio child,
so `LITELLM_MASTER_KEY` reaches the tool via inherited env — **no secret in the git-tracked
config.json**. `big` planning can take several minutes (deep reasoning) plus GPU swaps;
`PLAN_BUILD_TIMEOUT` (default 1800s) bounds the HTTP call. Verified live 2026-07-04:
`POST /plan-build/implement_spec` → code from `coder-next` in ~60s (incl. GPU swap).
The same script also serves these tools over **streamable-http on `:9100`** for the Copilot
CLI (`PLAN_BUILD_TRANSPORT=streamable-http`) — see [plan-build MCP over HTTP](#plan-build-mcp-over-http-for-copilot-byok).

To register in Open WebUI v0.10.2: **Settings → Integrations → External Tool Servers →
Add** → URL `http://<host-ip>:8000/<name>` (e.g. `http://<host-ip>:8000/time`; the IP
**must match the address your browser uses to reach Open WebUI** — LAN vs Tailscale),
Auth = Bearer `MCPO_API_KEY`. Gotchas: the Integrations row may not show a tool **count**
even when working (cosmetic); external tool servers do **not** appear in the `+` menu —
enable them per-chat via the **tools/🔧 icon next to `+`**; set the model's **Function
Calling = Native** (Workspace → Models → Advanced Params) for reliable invocation. Confirm
a call actually lands with `docker compose logs mcpo | grep 'Calling endpoint'`.
Coding harnesses (Copilot CLI, opencode, Claude Code) can also use the same MCP servers
natively over stdio (mcpo is only for HTTP/OpenAPI consumers).

**Secrets** (in `docker/.env`, gitignored; template `.env.example`): `WEBUI_SECRET_KEY`,
`SEARXNG_SECRET`, `MCPO_API_KEY`.

## Phase 6 (partial) — ComfyUI generative media (2026-07-03)

Headless **ComfyUI** for image generation, **native** (venv, ADR-0006) as a **burst V100**
workload (ADR-0010 — Pascal/P100 is too weak for SDXL/Flux). No X/display needed: it's a web
server on port **8188**; the canvas renders in your browser. Connect from a laptop over
LAN/Tailscale at `http://<host>:8188`.

**Layout**
- Repo: `/srv/ai/comfyui` (git clone of comfyanonymous/ComfyUI; **gitignored** like `src/`).
- Venv: `/srv/ai/venvs/comfyui` (Python 3.12). PyTorch **2.6.0+cu124** stock wheels — support
  V100 sm_70 (and P100 sm_60), so no custom build (unlike llama.cpp). Arch list = sm_50…sm_90.
- Models (`comfyui/models/checkpoints/`, gitignored): all-in-one checkpoints, loaded with the
  standard **Load Checkpoint** node:
  | file | ~size | use |
  |------|-------|-----|
  | `flux1-dev-fp8.safetensors` (Comfy-Org/flux1-dev, ungated) | 17 GB | FLUX.1-dev — quality |
  | `sd_xl_base_1.0.safetensors` (stabilityai, ungated) | 6.9 GB | SDXL base — speed |
  | `sd_xl_refiner_1.0.safetensors` (stabilityai, ungated) | 6.1 GB | SDXL refiner (optional 2nd pass) |

**Env-setup gotchas (Ubuntu 24.04 headless):**
- `python3.12-venv` is **not installed** → create the venv with `python3 -m venv --without-pip`
  then bootstrap pip via `get-pip.py` (same trick as the `hf` venv).
- ComfyUI's `comfy_kitchen`/Triton backend **JIT-compiles a CUDA helper at import** and needs
  the Python dev headers → **`sudo apt install python3.12-dev`** (provides `Python.h`), else
  startup dies with `fatal error: Python.h: No such file or directory`. `gcc` + `libcuda.so`
  are already present.

**Service** (`scripts/comfyui.service`, install via `sudo scripts/install-comfyui-service.sh`):
native systemd unit, `User=brad`, `CUDA_DEVICE_ORDER=PCI_BUS_ID`, pinned to **`CUDA_VISIBLE_DEVICES=1`**
(V100 #1). Installed but **not enabled at boot** (burst). `--listen 0.0.0.0 --port 8188`
exposes the UI on LAN/Tailscale — **ComfyUI has no auth**, so keep it on the private/Tailscale
network only. Start/stop: `sudo systemctl start|stop comfyui`.

**Auto-free the GPU (no manual step, keeps chat loaded).** A tiny ComfyUI server hook —
`scripts/comfyui-free-gpu-node.py`, installed to `comfyui/custom_nodes/free_gpu.py` — adds an
aiohttp middleware that, on **POST `/prompt`** (i.e. when someone clicks **Queue**), frees the
V100 ComfyUI needs, then runs the generation. ComfyUI is pinned to **idx1 (the `coding` card)**,
so the hook unloads **only** the model(s) squatting there via llama-swap's per-model endpoint
(`POST /api/models/unload/<model>`), while **keeping `chat` (idx2) + `fast` (P100) resident** so
family chat stays responsive during image gen. Models to keep are set by the `FREE_GPU_KEEP` env
(default `chat,fast`); everything else running (`coding`, or a split `big`/`coder-next`) is
evicted and reloads on-demand. It triggers **only on generate**, not on page loads. Configurable
via `LLAMASWAP_URL` + `FREE_GPU_KEEP` env in the unit. Verified: with `coding` resident
(idx1 = 32.1 GB), a queued run auto-unloaded **only** `coding` (chat/fast untouched) — log shows
`free_gpu: unloading ['coding'] before generation (keeping ['chat', 'fast'])`.

**Idle watchdog (reverse direction).** ComfyUI caches its models in VRAM after a run, which
would keep idx1 occupied and block `coding` from reloading. The same hook runs a background task
that, after `FREE_GPU_IDLE_SECS` (default 300s) with no generation, unloads ComfyUI's models
(`comfy.model_management.unload_all_models()` + `soft_empty_cache(force=True)`) to release idx1,
then warms `FREE_GPU_RESTORE` (default `coding`) back onto the card via llama-swap — so the box
returns to its daily resident state (coding + chat + fast) with no manual step. Runs at most once
per idle period; set `FREE_GPU_IDLE_SECS=0` to disable.

**Installing missing models/nodes (ComfyUI-Manager).** ComfyUI-Manager is installed into
`comfyui/custom_nodes/comfyui-manager` (the install script clones it + installs its deps into the
venv). It adds a **Manager** button in the web UI so the family can install missing models and
custom nodes from a curated list — **downloads happen server-side** into `/srv/ai/comfyui/models/`,
not on the client. (Clicking a raw download link in the plain "missing models" dialog would save to
the *laptop* and is useless; use the Manager button instead, or `hf-dl` on the server.) Config lives
at `comfyui/user/__manager/config.ini`; `security_level = normal` (default) allows curated model
installs even over LAN but blocks arbitrary git/pip installs from a remote browser. To allow those
too on the trusted LAN, set `allow_git_url_install = True` / `allow_pip_install = True` or lower
`security_level` (e.g. `normal-`) — only do this on the private/Tailscale network.

**Burst performance / VRAM.** Measured on a V100 (idx1), 1024×1024 @ 20 steps: **SDXL ~12 s**
(~10 GB), **Flux fp8 ~54 s** (~23 GB resident). Both verified end-to-end via the `/prompt` API.
Output images land in `comfyui/output/`. Caveat: if someone starts a chat *during* an active
image gen, llama-swap may try to reload a 31 GB LLM onto the busy card and briefly fail/queue —
rare in home use, and it resolves once the (short) generation finishes.

### ComfyUI MCP server — image gen as agent tools (ADR-0012)

Open WebUI's native ComfyUI integration only holds **one** workflow globally and its image
"Model" dropdown just swaps the *checkpoint* inside that one graph — it can't pick between
different graph topologies (Z-Image Turbo vs Flux vs a LoRA style). For **multiple styles** and
**agent / long-running** use, we expose ComfyUI as MCP tools via the vendored
[joenorton/comfyui-mcp-server](https://github.com/joenorton/comfyui-mcp-server).

- **Clone (updatable):** `/srv/ai/src/comfyui-mcp-server` (gitignored). Kept pristine except one
  local commit — a real bug fix (`_load_workflows` didn't skip `.meta.json` sidecars → startup
  crash; parity with `get_workflow_catalog`), pinned on upstream `e0101b2`, candidate to upstream.
  Update with `git -C /srv/ai/src/comfyui-mcp-server pull --rebase`.
- **Venv:** `/srv/ai/venvs/comfyui-mcp` (`--without-pip` + get-pip; deps `requests`, `mcp[cli]`,
  `Pillow`).
- **Service:** `scripts/comfyui-mcp.service` (install via
  `sudo scripts/install-comfyui-mcp-service.sh`). CPU-only bridge, `User=brad`, streamable-http on
  **`0.0.0.0:9000`**, talks to native ComfyUI at `127.0.0.1:8188`. Enabled at boot (cheap, no GPU).
  A tracked launcher `scripts/comfyui-mcp-launch.py` imports upstream's `mcp` object and (a)
  forces the bind host to `0.0.0.0` (upstream hard-codes 127.0.0.1) and (b) allows the mcpo
  `Host: host.docker.internal:9000` header (FastMCP's DNS-rebinding guard else returns **421
  Misdirected Request**) — **no fork of upstream**. Auth-less like ComfyUI → LAN/Tailscale only.
- **Style library (tracked in this repo):** `config/comfyui-mcp/workflows/` (set via
  `COMFY_MCP_WORKFLOW_DIR`), so styles are versioned and survive re-cloning. Each **API-format**
  `*.json` using `PARAM_<TYPE>_<NAME>` placeholders (e.g. `PARAM_PROMPT`, `PARAM_INT_SEED`,
  `PARAM_INT_WIDTH`) auto-registers its **own MCP tool** named after the file; an optional
  `*.meta.json` sidecar gives it a friendly name/description + parameter defaults. First style:
  **`z_image_turbo`** (4-step Lumina2/AuraFlow, 1024², cfg baked at 1). Add a style = drop in a
  new `<name>.json` (+ optional `.meta.json`); the tool `<name>` appears after a service restart.
- **Exposed via mcpo** (ADR-0011): `docker/mcpo/config.json` → `comfyui` entry, type
  `streamable-http`, url `http://host.docker.internal:9000/mcp`. **mcpo does not retry a backend
  that was down at its startup**, so the install script bounces mcpo after the service is up.
- **Tools:** per-style (`z_image_turbo`, …), plus `generate_image`, `run_workflow`,
  `list_workflows`, `list_models`, async jobs (`get_job`, `get_queue_status`, `cancel_job`),
  `regenerate`, `view_image` (inline base64), `list_assets`, `get_asset_metadata`,
  `get/set_defaults`, publish tools. Long renders return `{"status":"running","prompt_id":…}` —
  poll `get_job(prompt_id=…)`. Ideal for agents/long-running processes.
- **GPU coordination:** the tool hits ComfyUI's `/prompt`, so the existing `free_gpu` hook still
  fires (unloads idx1's LLM keeping chat+fast, idle watchdog restores `coding`) regardless of
  caller.
- **Displaying images inline:** mcpo serializes an MCP `ImageContent` (what `view_image`'s
  `FastMCPImage` produces) into an inert data-URI **string**; Open WebUI feeds that to the model
  as text and never renders it. So every generation tool response includes a **`markdown`** field
  (`![id](http://<server-LAN-ip>:8188/view?filename=…)`) and `view_image` returns the same — the
  model echoes it and OWUI renders the image. The URL is built from **`COMFY_MCP_PUBLIC_URL`**
  (set to the host's LAN/Tailscale address in `comfyui-mcp.service`; the bridge still *connects*
  to ComfyUI on localhost). It must match how the browser reaches the box (NOT
  `host.docker.internal`/`127.0.0.1`, which the browser can't resolve). `COMFY_MCP_RETURN_MARKDOWN=1`
  enables the markdown path. **If the server's LAN IP changes, update `COMFY_MCP_PUBLIC_URL`.**
- **Reliable inline embedding (recommended):** models sometimes present the link as a plain
  `[link](…)` (dropping the leading `!` → hyperlink, not image) or copy a **placeholder host**
  like `http://host:8188/…` (→ broken-image icon). The deterministic fix is an **OWUI Filter
  Function** — `docker/open-webui/functions/comfyui_inline_images.py` — whose `outlet` (a) rewrites
  any ComfyUI `/view?...filename=…` link or bare URL into `![](…)`, and (b) normalizes
  placeholder/loopback hosts (`host`, `localhost`, `127.0.0.1`, `host.docker.internal`) to the real
  browser-reachable address (Valve `comfyui_base_url`, default `http://<host-ip>:8188`).
  Model-independent. Install once: OWUI **Admin Panel → Functions → `+` (New Function)** → paste
  the file contents → Save → toggle it **on** (global). If your browser reaches the box via
  Tailscale, set the `comfyui_base_url` Valve accordingly.

Verified live 2026-07-05: `POST /comfyui/z_image_turbo {"prompt":…}` through mcpo (bearer
`MCPO_API_KEY`) → async job → `z-image-turbo_*.png` in `comfyui/output/`; all 18 tools listed at
`http://<host>:8000/comfyui/docs`. Register in Open WebUI the same way as other mcpo tools
(Settings → Integrations → External Tool Servers → `http://<host-ip>:8000/comfyui`).

## Personal-assistant gateways — OpenClaw + Hermes (2026-07-21)

Two self-hosted **always-on assistant gateways** run as app-tier containers
(compose `/srv/ai/docker/`), both talking to the native models through the
LiteLLM gateway (`host.docker.internal:4000`). They are the front door of the
"assistant" layer; heavier work is delegated to the existing tiers (n8n
automations, Copilot CLI / coding models, ComfyUI + plan-build + llama-swap-mode
MCPs). See **ADR-0016** for the layered decision and framework comparison
(OpenClaw vs Hermes vs pi.dev).

| Service   | Image                                  | Access                | Purpose |
|-----------|----------------------------------------|-----------------------|---------|
| openclaw  | `ai-server/openclaw:2026.7.1` (built from `ghcr.io/openclaw/openclaw:2026.7.1` + skill-dep binaries, see `docker/openclaw/Dockerfile`) | `http://localhost:18789` (Control UI needs a secure context — reach it via SSH tunnel: `ssh -N -L 18789:127.0.0.1:18789 <user>@<host>`) | Multi-channel assistant gateway + Control UI |
| hermes    | nousresearch/hermes-agent:latest       | `http://<host>:9119` (dashboard), `:8642` (API) | Agentic assistant (self-improving skills) |

**Model wiring (both):** primary `chat` (always-warm MoE), fallback `coding`,
utility/small tasks `fast` — i.e. the daily-mode trio, so no GPU swap on normal
use. All authenticate to LiteLLM with `LITELLM_MASTER_KEY`.

**One-time setup (host, non-privileged):**
```bash
cd /srv/ai/docker
cp .env.example .env   # if not already; fill in the assistant secrets (below)
../scripts/assistants-seed.sh          # seeds gitignored /srv/ai/{openclaw,hermes}
docker compose up -d openclaw hermes
```
`assistants-seed.sh` is idempotent: it creates the runtime dirs (uid/gid 1000,
matching `brad`) and seeds each config **only if missing** (so agent-written
state, schema migrations, and learning-loop skills survive). It copies the
tracked templates `docker/openclaw/openclaw.json` + `docker/hermes/config.yaml`
and injects the LiteLLM key into Hermes' live config.

**OpenClaw** (`ghcr.io/openclaw/openclaw`, Node daemon, runs as `node`/uid 1000):
- Config = JSON5 at `/srv/ai/openclaw/state/openclaw.json` (writable; OpenClaw runs
  schema migrations). A dedicated `litellm` provider (`api: openai-completions`,
  `baseUrl: http://host.docker.internal:4000`, `apiKey: "${LITELLM_API_KEY}"`,
  `request.allowPrivateNetwork: true`) lists `chat`/`coding`/`fast`;
  `agents.defaults.model.primary = litellm/chat`, `fallbacks = [litellm/coding]`.
- **Must** set `gateway.mode: "local"`, `gateway.bind: "lan"` and an
  `OPENCLAW_GATEWAY_TOKEN` (env SecretRef) — a loopback bind makes the published
  port unreachable; a LAN bind without a token is refused.
- The canonical config was generated with
  `openclaw onboard --non-interactive --accept-risk --auth-choice custom-api-key
  --custom-provider-id litellm --custom-base-url http://host.docker.internal:4000/v1
  --gateway-bind lan --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN` then patched
  with the 3-model roster. `OPENCLAW_SKIP_ONBOARDING=1` keeps the container
  declarative. Diagnose config issues with `docker compose exec openclaw node
  openclaw.mjs doctor`. Three persistent dirs: `state`, `workspace`, `auth-secrets`.
- Verified live 2026-07-21: `openclaw agent --agent main -m "…"` →
  `winnerProvider: litellm, winnerModel: chat, result: success`.
- **Control UI needs a secure browser context** (WebCrypto device identity):
  reach it over an SSH tunnel to `http://localhost:18789`, not the LAN hostname.
  `gateway.controlUi.allowInsecureAuth` only helps an on-host (loopback) browser;
  a remote LAN browser additionally fails the `isLocalClient` check.
- **Update the pinned image tag deliberately** (`docker compose pull` + `up -d`),
  never the in-app self-updater (ephemeral container layer). The auto-updater is
  off by default. The "Update available" startup banner checks the **npm** registry
  (`2026.7.1-2`), whose `-N` patch suffix is **not** published as a GHCR image tag,
  so it's a cross-channel false positive for the container — silenced with
  `update.checkOnStart: false`. GHCR ships the base `2026.7.1` release image.
- **Skills are allowlisted** to cut error-state noise. The image bundles ~53 skills;
  most sit in "needs setup" because they need macOS APIs (apple-notes, things-mac,
  peekaboo, sonos…) or personal cloud-account CLIs (1password, trello, xurl…) that
  can't work on this headless Linux box. `skills.allowBundled` is an **allowlist**
  (managed/workspace skills unaffected) pinned to the 19 that work here — the 15
  dependency-free ready ones plus `video-frames`, `session-logs`, `github`,
  `gh-issues`. `allowBundled` blocks the rest from the agent but does **not** flip
  the Control-UI enabled toggle, so the 34 unwanted skills are *also* set
  `skills.entries.<key>.enabled: false` (what the UI reads → they show disabled).
  Result: **skills Errors: 0**.
- **A few skill deps are baked into a local image extension** rather than installed
  at runtime (the in-app "install dependency" button targets `/usr/local` in the
  ephemeral layer and even suggests `brew`, so it won't persist and the container is
  non-root). `docker/openclaw/Dockerfile` extends the pinned upstream image with
  `ffmpeg` (video-frames), `jq` + `ripgrep` (session-logs needs both) and `gh`
  (github/gh-issues); compose builds it as `ai-server/openclaw:<tag>`. **User-installed
  skill *content*** (ClawHub/git) already persists under the mounted `state` dir
  (`~/.openclaw/plugin-skills`, `skill-workshop`) — no extra volume needed; only
  dependency *binaries* need the image extension. Bump `BASE_IMAGE` in the Dockerfile
  in lockstep with the compose tag when updating, then `docker compose build openclaw`.

**Hermes** (`nousresearch/hermes-agent`, Nous Research; s6-overlay PID 1):
- **TUI-first** but runs headless here via `command: ["gateway", "run"]`; the web
  dashboard (`:9119`, basic-auth) and OpenAI-compatible API server (`:8642`,
  key-gated) are enabled by env. Single state volume `/srv/ai/hermes` → `/opt/data`
  (config, `.env`, sessions + FTS5 DB, memory, **agent-written skills**).
- Config = `/opt/data/config.yaml`: `model.provider: custom`,
  `base_url: http://host.docker.internal:4000/v1`, `default: chat`, `api_key` =
  the LiteLLM master key (injected by the seed script; the tracked template holds
  a placeholder). `OPENAI_BASE_URL` is **not** honored for the `custom` provider.
- Do **not** pass `user:` — Hermes remaps its internal user via
  `HERMES_UID`/`HERMES_GID` (both `1000`); `--user` breaks the s6 tree.
- The self-improving skill loop **executes code inside the container** (isolation
  is why we containerize). The `:8642` API + `terminal.backend: local` means keyed
  callers run agent work as the container user — keep it LAN/firewalled.
- Verified live 2026-07-21: `POST :8642/v1/chat/completions {model:"hermes-agent"}`
  → `"pong"` (drove `chat` via LiteLLM).

**Secrets** (in `docker/.env`, gitignored; template `.env.example`):
`OPENCLAW_GATEWAY_TOKEN`, `HERMES_DASHBOARD_USER`/`HERMES_DASHBOARD_PASSWORD`,
`HERMES_API_SERVER_KEY` (generate the tokens with `openssl rand -hex 32`). Both
reuse `LITELLM_MASTER_KEY` for the model backend.

## Network exposure & firewall (2026-07-07)

The AI services bind `0.0.0.0` and are meant for the **trusted LAN + Tailscale
only** — they must never be port-forwarded to the public internet. Auth posture:

| Service | Port | Auth |
| --- | --- | --- |
| ComfyUI **open** (`comfyui-open`) | 8188 | **none** — do NOT expose to WAN |
| ComfyUI **locked** (`comfyui-secure`) | 8189 | ComfyUI-Login (basic password only) |
| Open WebUI | 3000 | app login |
| Filebrowser (ComfyUI media) | 8083 | own login (change admin/admin on first visit) |
| LiteLLM gateway | 4000 | `LITELLM_MASTER_KEY` |
| mcpo | 8000 | `MCPO_API_KEY` |
| OpenClaw gateway | 18789 | `OPENCLAW_GATEWAY_TOKEN` (+ bridge 18790) |
| Hermes dashboard | 9119 | basic auth (`HERMES_DASHBOARD_*`) |
| Hermes API server | 8642 | `HERMES_API_SERVER_KEY` |
| SearXNG | 8888 | none |
| llama-swap mgmt | 127.0.0.1:9090 | localhost-only (safe) |

The box sits behind home-router NAT (`eno1 = <host-ip>/22`), so nothing is
WAN-reachable unless the router forwards a port. `ufw` (installed) adds
defense-in-depth: allow only LAN + Tailscale, deny everything else. Run with sudo
(agents cannot):

```bash
# Order matters — add the allow rules BEFORE enabling, or you can lock out SSH.
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0            # trust the Tailscale mesh
sudo ufw allow from <lan-subnet>            # trust the local LAN (all ports)
# (LAN rule already covers SSH; if you tighten it, keep: sudo ufw allow 22/tcp)
sudo ufw enable
sudo ufw status verbose
```

This keeps the no-auth `:8188` (and everything else) reachable from your LAN and
Tailscale devices while blocking any other source. If you ever must reach a
service from outside, prefer Tailscale over a router port-forward — never expose
`:8188`. To reset the locked instance's password:
`sudo /srv/ai/scripts/reset-comfyui-password.sh` (see ADR-0013).

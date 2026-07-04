# Headless AI Server — Setup Notes & Plan

Personal headless AI server for development and AI workloads. Copilot CLI currently
runs directly on the box; eventually access will be via OpenAI-style API(s) only.

> **Design decisions** are recorded as ADRs in [`adr/`](adr/README.md).
> **Serving architecture** (topology, VRAM budget, routing table, rollout) is in
> [`architecture.md`](architecture.md).

Last updated: 2026-06-30

---

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

### vLLM  (likely needed later — set expectations)
- Higher throughput + better continuous batching, **but Volta support is second-class
  and shrinking**: no prebuilt Volta-optimized path, no FlashAttn2/FP8/Marlin on sm_70,
  fp16-only. May require **building from source** against a matching torch/CUDA 12.x.
- **P100 (sm_60) is effectively unsupported** by current vLLM — keep vLLM to the V100s.
- Alternatives worth evaluating: **SGLang**, **TGI**, **ExLlamaV2** (verify it still
  supports sm_70), or sticking with llama.cpp if throughput is adequate.

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

## Coding-model benchmark — Qwen3.6-27B on the V100s (2026-07-01)
Model: `Qwen3.6-27B` (dense, hybrid linear+full attention, `qwen35` arch — see
ADR-0008). GGUFs from `unsloth/Qwen3.6-27B-GGUF` in `/srv/ai/models/qwen3.6-27b/`.
Bench: `scripts/bench-qwen3.6-27b.sh` (llama-bench, -p512 -n128 -r3, depths 0/8192).
Raw: `/srv/ai/models/qwen3.6-27b/bench-*/results.md`.

**tg128 = token-gen t/s (interactive speed); pp512 = prompt-processing t/s.**

| Quant / config          | pp512 | tg128 | pp @8k | tg @8k |
|-------------------------|------:|------:|-------:|-------:|
| Q6_K  single V100       |  870  | 25.6  |  748   | 22.7   |
| Q6_K  dual — layer      |  873  | 25.6  |  754   | 24.6   |
| Q6_K  dual — row        |  203  | 21.4  |  195   | 20.4   |
| BF16  dual — layer      |  183  | 12.1  |  163   |  9.6   |
| BF16  dual — row        |  193  | 12.2  |  162   |  9.7   |

**Findings (answers the ADR-0005 TP question):**
- **Splitting a model that fits one card gives ~no throughput benefit.** Q6_K
  single vs dual-layer is a tie (~25.6 tg). Dual's value is *capacity*, not speed.
- **`-sm row` is bad on this box:** ~4× slower prompt processing (203 vs 872 pp)
  from per-layer PCIe sync (no NVLink). **Use `-sm layer` (default), never `row`.**
- **BF16 needs both cards and runs ~2× slower than Q6_K** (12 vs 25.6 tg) for a
  marginal quality gain → not worth it for serving.
- **Dual-layer helps slightly at depth** (24.6 vs 22.7 tg @8k): KV cache spread
  over 2 cards eases the memory-bandwidth hit as context grows.

**Serving recommendation:** run **Q6_K on a single V100** (`-sm none`,
`CUDA_VISIBLE_DEVICES=1`), leaving V100 #2 free for a second model (e.g. the
35B-A3B MoE or a 2nd instance). Only tensor-split (layer) when a model/context
genuinely won't fit on one card.

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

## MoE benchmark — Qwen3.6-35B-A3B on the V100s (2026-07-01)
Model: `Qwen3.6-35B-A3B` (MoE, 34.66B total / ~3B active, `qwen35moe` arch).
GGUF `unsloth/...UD-Q6_K` in `/srv/ai/models/qwen3.6-35b-a3b/`.
Bench: `scripts/bench-qwen3.6-35b-a3b.sh`. Raw: `.../bench-*/results.md`.

| Quant / config          | pp512 | tg128 | pp @8k | tg @8k |
|-------------------------|------:|------:|-------:|-------:|
| Q6_K  single V100       |  773  | 97.6  |  697   | 95.2   |
| Q6_K  dual — layer      |  755  | 97.1  |  704   | 95.0   |
| Q6_K  dual — row        |  467  | 42.1  |  438   | 41.4   |
| BF16  dual (layer/row)  |  — DID NOT FIT (weights ~69 GB > 64 GB VRAM) — |

**Findings:**
- **MoE is ~3.8× faster than the dense 27B** (97.6 vs 25.6 tg t/s) — only ~3B of
  35B params active per token. Big win for latency/interactive use.
- Single vs dual-layer = tie again (~97 tg): confirms splitting a model that fits
  one card yields no throughput gain (dual = capacity, not speed).
- **`-sm row` is even worse for MoE**: tg halves (42 vs 97) — expert routing +
  per-layer PCIe sync. Never use row on this box.
- **BF16 MoE won't run**: 69 GB weights > 64 GB (2×V100). Q6_K (27 GB) fits ONE
  card and is the practical max-quality config; Q8_0 (37 GB) would need both cards
  if higher precision is ever wanted.

**Serving rec:** run **35B-A3B Q6_K on a single V100** for a fast, low-latency
model — pairs well with the dense 27B Q6_K on the other V100 (one card each).

### Uncensored fine-tune smoke test — Qwen3.6-35B-A3B-Uncensored (HauhauCS-Aggressive, 2026-07-01)
Model: `HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive` (same `qwen35moe`
arch, uncensored fine-tune, **reasoning model** with a vision mmproj available).
GGUFs in `/srv/ai/models/qwen3.6-35b-a3b/`. Live `llama-server` smoke test (not
llama-bench), single short request, cards under the **175 W cap**.

| Quant       | Size    | Layout                | VRAM        | pp t/s  | tg t/s | result |
|-------------|--------:|-----------------------|-------------|--------:|-------:|--------|
| Q4_K_M      | 21.2 GB | 1× V100 (idx1)        | 20.7 GB     | 147-196 | ~102   | ✓ correct |
| Q6_K_P      | 30.6 GB | 2× V100 (`-sm layer`) | 14.7+15.5 GB| ~107    | ~93    | ✓ correct |

**Findings:**
- **Q4_K_M on a single V100 is the practical default** — ~102 tg t/s, leaves the 2nd
  V100 free and ~11 GB headroom for context. Matches the ~97 tg of the unsloth Q6_K
  above (MoE speed is active-param-bound, not quant-bound).
- **Q6_K_P (30.6 GB) does NOT fit one V100 with usable context** → needs both cards
  via `-sm layer` (14.7+15.5 GB, well balanced). Costs the 2nd card + ~10% tg (93 vs
  102) for the higher-quality quant; the drop is PCIe cross-GPU traffic (PHB, no
  NVLink). Use only when Q6 quality is specifically wanted.
- **Reasoning model**: emits a thinking block first. Final answer is in the response
  `content`; chain-of-thought is in `reasoning_content`. Even a 3-word reply burns
  ~100-200 completion tokens on reasoning — budget `max_tokens` generously (≥256), or
  disable thinking (`/no_think` in the prompt, or `enable_thinking:false` template flag).
- Downloaded via the keyring-backed wrapper `scripts/hf-dl` (Xet backend, byte-exact).
- Temps stayed ~41 °C — a single short request doesn't stress the cards; sustained
  load would behave like the other 35B-A3B results above.

## Tensor-parallel / multi-GPU reality (measured 2026-07-01)
`nvidia-smi topo -m`: all GPU pairs = **PHB** (PCIe via CPU host bridge), **no NVLink**.
P2P test (`/tmp/p2ptest.cu`, cudaMemcpyPeer, 256MB) between the two V100s:
- **P2P peer access: ENABLED** both directions.
- **Inter-GPU bandwidth: ~5.2 GB/s** (vs NVLink 25-300 GB/s) — routed over PCIe
  gen3 through the CPU. This is the ceiling for any all-reduce.

**What "tensor parallelism" means in our tests:**
- llama.cpp **`-sm row` = tensor split** (splits each weight matrix + per-layer
  all-reduce). Tested: 4x slower prefill (dense), ~2x slower tg (MoE). This is the
  no-NVLink penalty hitting the 5.2 GB/s link every layer.
- llama.cpp **`-sm layer` = pipeline** (layers split across cards, tiny traffic).
  Tested: matches single-card speed.

**Conclusion:** TP *works* on the 2xV100 (P2P on, same sm_70) but is
**communication-bound**. Use it for **capacity** (models >32GB), not speed. For
single-stream latency, prefer **one model per card**. vLLM's NCCL TP=2 is more
optimized than llama.cpp row-split and *may* help under **batched/concurrent**
serving — retest when vLLM is brought up. P100 cannot join TP (arch/mem mismatch).

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
`gpu-fan-control.py:apply_power_limits()` runs `nvidia-smi -i N -pm 1 -pl W` for each entry
at startup (logged as `power cap GPUN -> W W`). A capping failure is **non-fatal** — the
fan daemon keeps running (airflow is the safety-critical function). To change caps, edit the
config and re-run `sudo install-fan-service.sh` (validates 50-400 W range). Board limits:
V100 100-250 W, P100 125-250 W.

## Phase 2 — llama-swap model router (2026-07-02)

Native on-demand model router in front of `llama-server`, OpenAI-compatible. Binary
`/srv/ai/bin/llama-swap` (v234); config `/srv/ai/config/llama-swap.yaml`; systemd unit
`llama-swap.service` (binds `127.0.0.1:9090`, runs as `brad`, `-watch-config`).
Install/update: `sudo /srv/ai/scripts/install-llama-swap-service.sh`.

**Models / GPU map** (`CUDA_DEVICE_ORDER=PCI_BUS_ID`; idx0=P100, idx1/2=V100):

| model    | file                               | GPU(s)      | ctx   | VRAM   |
|----------|------------------------------------|-------------|-------|--------|
| `coding` | Qwen3.6-27B **Q6_K**               | idx1        | 163840 | ~31.5 GB |
| `chat`   | Qwen3.6-35B-A3B **UD-Q6_K**        | idx2        | 16384 | ~28 GB (q8_0 KV) |
| `big`    | Qwen3.6-27B **BF16** (split)       | idx1+idx2   | 16384 | ~51 GB (25+26), ttl 300s |
| `fast`   | **Gemma-4-12B** QAT UD-Q4_K_XL     | idx0 (P100) | 131072 | ~10.8 GB, always-on, `--reasoning-budget 0`, ub2048 |
| `gemma-31b` | **Gemma-4-31B** QAT UD-Q4_K_XL  | idx1        | 131072 | ~26 GB (q8_0 KV), ttl 600s (evicts coding), ub2048 |
| `gemma-26b` | **Gemma-4-26B-A4B** MoE QAT     | idx2        | 131072 | ~18 GB, ttl 600s (evicts chat), ub2048 |

**Routing = matrix (3 cards).** `f`(fast, P100) is in every set so it's never evicted and runs
CONCURRENTLY with the V100 models. V100 sets: `qq: c & h & f` (daily), `qg: c & y & f`,
`gq: x & h & f`, `gg: x & y & f` (any Qwen/Gemma pairing across idx1/idx2), `max: b & f`
(big splits both V100s). Verified 2026-07-02: coding(idx1)+chat(idx2)+fast(P100) all
co-resident (31.8/28.4/7.7 GB); `fast` answers immediately (Gemma reasoning disabled).

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

## GitHub Copilot CLI via BYOK (2026-07-02)

Copilot CLI supports OpenAI-compatible endpoints (BYOK). It points at the LiteLLM gateway.
Requirements (both verified through the full stack 2026-07-02): **tool calling** (Qwen3.6 +
`--jinja` → `finish_reason: tool_calls`) and **streaming** (SSE). Docs recommend a ≥128k context
window for best results; our `coding` model is currently 32768 (tunable).

Env vars (see `scripts/copilot-byok.sh`, which sources the key from `docker/.env`):

    export COPILOT_PROVIDER_BASE_URL=http://<host>:4000/v1   # e.g. Tailscale <tailscale-ip>
    export COPILOT_PROVIDER_TYPE=openai
    export COPILOT_PROVIDER_API_KEY=$LITELLM_MASTER_KEY
    export COPILOT_MODEL=coding        # or chat / big
    copilot

On the server just run `/srv/ai/scripts/copilot-byok.sh`. If the endpoint 404s, try the base URL
without the trailing `/v1`.

### coding context-window sweep (2026-07-02)

Qwen3.6-27B Q6_K on one V100-32GB, `--parallel 1 --flash-attn on`, f16 KV. Model's trained
context is 262144 (256k), so VRAM is the limit. KV grows ~65 MB per 1k tokens; the flash-attn
compute buffer is fixed (scales with u-batch, not prompt length), so load-time VRAM ≈ peak.

| ctx     | VRAM used | free    | notes                                   |
|---------|-----------|---------|-----------------------------------------|
| 32768   | ~23.3 GB  | ~9.4 GB | previous default                        |
| 131072  | 29.4 GB   | 3.3 GB  | meets Copilot BYOK ≥128k recommendation |
| 163840  | 31.5 GB   | 1.25 GB | **chosen** — practical max with f16 KV  |
| ≥172032 | —         | —       | exceeds 32 GB (would OOM)               |

Chose **163840 (160k)**. Switched coding to `--parallel 1` so the full window serves one agent
(concurrent coding requests serialize — fine for personal use). To reach the model's full 256k,
use `--cache-type-k q8_0 --cache-type-v q8_0` (halves KV, slight quality trade-off).

### Prompt-processing (prefill) tuning — `--ubatch-size` (2026-07-02)

Raising `--ubatch-size` (`-ub`, default 512) speeds **prefill / time-to-first-token** (helps
large prompts, e.g. tool results injected into context). It does **not** change generation
speed. Cost = a larger CUDA compute buffer (VRAM). `llama-bench` on a V100:

| model              | -ub 512 | -ub 1024 | -ub 2048 | applied |
|--------------------|---------|----------|----------|---------|
| coding (27B Q6_K, 1×V100) | 746 t/s | **858 (+15%)** | 892 (+20%) | **`-ub 1024`** — 2048 nearly OOMs at ctx 163840 (~0.4 GB free) |
| chat (35B-A3B UD-Q6_K, 1×V100) | — | — | +~20% | **`-ub 2048`** — has ~4 GB headroom |
| big (27B BF16, 2×V100 layer-split) | **232 t/s** | 205 | 167 | **default 512** — larger *hurts* (inter-GPU sync) |

Key lesson: bigger `-ub` helps single-GPU models but **hurts layer-split multi-GPU** models.
`coding` at `-ub 1024` uses ≈ the same VRAM as 512 (free +15%). Verified both load without OOM.

### Gemma-4 benchmarks + context/ubatch tuning (2026-07-02)

`llama-bench` (`-p 2048 -n 128`, flash-attn on, `CUDA_DEVICE_ORDER=PCI_BUS_ID`). **Note:** without
`CUDA_DEVICE_ORDER=PCI_BUS_ID`, CUDA orders devices by *speed* (V100s first, P100 last) — the
opposite of nvidia-smi/llama-swap — so always export it when pinning a card for benchmarks.

**Throughput** (t/s):

| model | card | pp2048 ub512 | ub1024 | ub2048 | tg128 |
|-------|------|--------------|--------|--------|-------|
| Gemma-4-12B (dense) | **P100** | 368 | 324 | 458 | **30** |
| Gemma-4-12B (dense) | **V100** | 1526 | 1814 | **1987** | **71** |
| Gemma-4-31B (dense) | V100 | 583 | 697 | **760** | 34 |
| Gemma-4-26B-A4B (MoE) | V100 | 1486 | 1887 | **2269** | **110** |

- **P100 vs V100 (12B):** the V100 is ~4.3× faster prefill and ~2.35× faster generation. `fast`
  stays on the P100 anyway (frees both V100s for the big Qwen/Gemma models); 30 t/s is fine for
  chat, and the P100 is otherwise idle.
- **26B-A4B MoE is the fastest model on the box** — 110 t/s gen (only ~3.8B active params),
  beating even the dense 12B. Best quality/speed Gemma for daily use.
- **ubatch:** `-ub 2048` is optimal prefill for *all* single-GPU Gemmas (dense +28-30%, MoE +53%).
  Applied `-ub 2048` to `fast`, `gemma-31b`, `gemma-26b`.

**Context / VRAM.** All three Gemma-4 models are **256K-native** (`context_length 262144`) and use
**sliding-window attention** (1024 window, 5 SWA : 1 global layer), so KV cache grows very slowly —
only the 1-in-6 global layers hold full-length KV. Measured resident VRAM (f16 KV, `-ub` default):

| model | ctx 32k | 65k | 131k | 262k (full) | applied ctx |
|-------|---------|-----|------|-------------|-------------|
| 12B / P100 16GB | 8.8 | 9.3 | 10.4 | 12.6 GB | **131072** (10.8GB @ub2048; leaves P100 aux room) |
| 31B / V100 32GB | 23.2 | 25.8 | 31.0 | OOM | **131072** (q8_0 KV → 26.7GB @ub2048; f16 OOMs at 131k) |
| 26B-A4B / V100 32GB | 15.6 | 16.3 | 17.6 | 20.3 | **131072** (18.0GB @ub2048; full 256k also fits) |

Full 256K only costs +2-4 GB over 16K thanks to SWA. **31B needs `--cache-type-k/v q8_0`** (halves
KV, needs flash-attn) to reach 128k — f16 KV at 131k hits 31GB + compute buffer and OOMs; q8_0
brings it to ~26.7GB. The 12B and 26B-A4B have room to spare with f16 KV. Verified all three
co-resident after tuning: P100 10.8GB / V100#1 26GB (31B@131k) / V100#2 18.0GB, all answering.

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
`implement_spec` (implement a given spec directly with `coder-next`, no planning). Planner
(`big`/`chat`/`fast`) and coder are overridable per call. Because `big` and `coder-next`
both need the two V100s, a `plan_and_build` call swaps `big` in (evicting coding+chat), then
`coder-next` in (evicting `big`); `fast` stays resident so the chat keeps responding. **This
needs container networking + the gateway key:** the mcpo service adds
`extra_hosts: host.docker.internal:host-gateway` (LiteLLM runs `network_mode: host` on
`:4000`) and `env_file: ./.env`; mcpo passes `{**os.environ, **cfg.env}` to the stdio child,
so `LITELLM_MASTER_KEY` reaches the tool via inherited env — **no secret in the git-tracked
config.json**. `big` planning can take several minutes (deep reasoning) plus GPU swaps;
`PLAN_BUILD_TIMEOUT` (default 1800s) bounds the HTTP call. Verified live 2026-07-04:
`POST /plan-build/implement_spec` → code from `coder-next` in ~60s (incl. GPU swap).

To register in Open WebUI v0.10.2: **Settings → Integrations → External Tool Servers →
Add** → URL `http://<host-ip>:8000/<name>` (e.g. `http://192.168.4.57:8000/time`; the IP
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

**Auto-free the GPU (no manual step).** A tiny ComfyUI server hook —
`scripts/comfyui-free-gpu-node.py`, installed to `comfyui/custom_nodes/free_gpu.py` — adds an
aiohttp middleware that, on **POST `/prompt`** (i.e. when someone clicks **Queue**), first calls
llama-swap `GET /unload` to free the V100, then runs the generation. So a family member just
opens the UI and hits generate; the coding/chat models are evicted automatically and reload
on-demand the next time they're used. It triggers **only on generate**, not on page loads, so
merely opening the tab does not disturb chat. Configurable via the `LLAMASWAP_URL` env in the
unit. Verified: with `coding` resident (idx1 = 32.1 GB), a queued SDXL run auto-unloaded it and
completed — log shows `free_gpu: unloading llama-swap models before generation: coding`.

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

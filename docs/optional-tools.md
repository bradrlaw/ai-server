# Optional add-on tools (training + alternative image-gen UIs)

These are **optional** OSS tools installed alongside — not replacing — ComfyUI and
the LLM serving stack. Rationale, isolation strategy and the port map are in
[ADR-0020](adr/0020-additional-oss-creative-tools.md). If you are following this
repo as a build guide, install any subset you like; the core stack does not depend
on them.

**Common ground rules on this hardware (Tesla V100/P100, sm_70/sm_60, CUDA 12.x):**
- Each tool gets its **own venv** and its **own port** — nothing shares a Python
  environment or process with llama-swap / ComfyUI.
- Each tool must run on the **cu124 / torch 2.6.0** wheel (the ComfyUI-proven build
  whose kernel list includes `sm_70`/`sm_60`). The tools' own installers increasingly
  pin **CUDA 13.x / torch ≥2.7** wheels that **drop Volta/Pascal kernels** — those
  will fail with *"no kernel image is available for execution on the device"*. See
  ADR-0003.
- Services are native systemd units under `scripts/`. The agent can't `sudo`, so
  **you install/enable them by hand** (commands below).

| Tool | Purpose | Port | Status |
|------|---------|------|--------|
| [ai-toolkit](#ai-toolkit-training) | model **training** (LoRA/fine-tune) | 8675 | installed |
| Fooocus | simple image gen | 7865 | installed |
| SwarmUI | image gen (ComfyUI-backed) | 7801 | planned |
| InvokeAI | image gen (canvas/pro) | 9091 | planned |

---

## ai-toolkit (training)

**What it is.** [Ostris **AI Toolkit**](https://github.com/ostris/ai-toolkit) — an
all-in-one training suite for diffusion image/video models (LoRA and fine-tunes)
with a web UI for starting/monitoring jobs. Created and maintained by **Ostris**;
licensed **MIT**. This is our **training** tool (the other three add-ons are for
generation).

**Why these versions (the Volta caveat).** ai-toolkit's own installer (the
"AI Toolkit Manager", and its manual `requirements`) pins **torch 2.13.0 + cu130**
aimed at Blackwell/sm120. CUDA 13.x wheels dropped the `sm_70`/`sm_60` kernels, so
the stock install cannot run on our V100/P100. We install against **torch 2.6.0 +
cu124** instead and reconcile the torch-version-locked deps to that line:

| Upstream pin | Installed here | Why |
|---|---|---|
| `torch 2.13.0+cu130` (via manager) | `torch 2.6.0+cu124` | cu130 has no sm_70 kernels; 2.6.0+cu124 does |
| `torchcodec==0.9.1` | `torchcodec==0.3.0` | 0.3.0 is the torch-2.6 release |
| `torchao==0.10.0` | `torchao==0.9.0` | 0.9.0 matches the torch-2.6 line |
| `transformers 5.5.3`, `diffusers`@git | kept | import & run fine on torch 2.6 |

A constraints file hard-pins the torch trio during `pip install` so no transitive
dep silently pulls a newer (sm_70-less) torch back in.

**Install (reproducible).** Everything above is codified in
[`scripts/install-ai-toolkit.sh`](../scripts/install-ai-toolkit.sh):

```bash
/srv/ai/scripts/install-ai-toolkit.sh
```

It clones to `/srv/ai/ai-toolkit`, creates `/srv/ai/venvs/ai-toolkit`
(`--without-pip` + get-pip.py, because `python3-venv` isn't installed system-wide —
same trick as the ComfyUI venv), installs the reconciled deps, symlinks
`ai-toolkit/venv → venvs/ai-toolkit` (the UI worker looks for `TOOLKIT_ROOT/venv/
bin/python`), and builds the Next.js UI (prisma SQLite DB at
`/srv/ai/ai-toolkit/aitk_db.db`).

**Run it (port 8675).**

```bash
# one-off, foreground
cd /srv/ai/ai-toolkit/ui && npm run start
```

As a service (needs sudo — hand-installed like the ComfyUI units):

```bash
sudo cp /srv/ai/scripts/ai-toolkit-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-toolkit-ui
# logs: journalctl -u ai-toolkit-ui -f
```

The UI is then at `http://<server>:8675`. It appears on the
[status page](server-setup.md) Services panel ("AI Toolkit").

**Auth.** The UI leaves pages open but 401s the `/api/*` routes when
`AI_TOOLKIT_AUTH` is set. To require a token (recommended if reachable beyond
loopback/Tailscale), create `/srv/ai/ai-toolkit/ui.env`:

```ini
AI_TOOLKIT_AUTH=some-long-random-token
HF_TOKEN=hf_xxx   # optional, for gated models like FLUX.1-dev
```

**GPU / device.** The service sets `CUDA_DEVICE_ORDER=PCI_BUS_ID`, so a job's
`device: cuda:1` / `cuda:2` maps to the V100s as numbered by `nvidia-smi`
(idx0=P100, idx1/2=V100). Training is heavy — run it when the target V100 isn't
busy serving an LLM (e.g. switch llama-swap to a mode that frees idx2, or stop the
relevant model). The 32 GB V100s can do FLUX.1 / SDXL LoRA; the P100 (16 GB) is too
small for most modern training.

**Limitations on Volta.**
- **Video/audio training** (Wan, MiniMax-H3, Ace-Step) needs system FFmpeg for
  `torchcodec`: `sudo apt install ffmpeg`. Image LoRA training does **not** — the
  torchcodec import is lazy (only when a video/audio dataset loads).
- No fp8 / FlashAttention-2 on sm_70; the newest models (e.g. FLUX.2) that assume
  those may not train. Stick to FLUX.1 / SDXL / SD1.5 / Qwen-Image for reliable runs.

**Attribution.** Ostris AI Toolkit © Ostris, MIT License —
<https://github.com/ostris/ai-toolkit>. Consider supporting the project via
[Ostris Cloud](https://cloud.ostris.com).

---

## Fooocus (image gen)

**What it is.** [**Fooocus**](https://github.com/lllyasviel/Fooocus) by
**lllyasviel** (Lvmin Zhang, author of ControlNet/Forge) — a deliberately
minimal, "just type a prompt" SDXL image generator. No node graph, few knobs:
the opposite of ComfyUI, aimed at family members who want good SDXL images with
zero setup. Under the hood it's a vendored 2024 ComfyUI fork (`ldm_patched`).
Licensed **GPL-3.0**.

**Why these versions (the Volta + web-stack caveats).** Two stock-install traps on
this box, both fixed by the installer:

| Upstream default | Installed here | Why |
|---|---|---|
| `torch==2.1.0` (launch.py) | `torch 2.6.0+cu124` | 2.1.0 has no cp312 wheels (we're on Py 3.12); cu124/2.6.0 keeps sm_70/sm_60 |
| `torchvision` (implicit) | `torchvision==0.21.0` | matches the torch-2.6 line |
| `fastapi`/`starlette` *(unpinned)* | `fastapi==0.103.2`, `starlette==0.27.0`, `anyio==3.7.1` | a 2026 index resolves these to starlette ≥1.x, which breaks gradio 3.41.2 with *"Jinja2Templates unhashable type: dict"* at UI launch |
| `pydantic` *(unpinned)* | `pydantic==2.4.2`, `pydantic-core==2.10.1` | pydantic ≥2.5 breaks fastapi 0.103 with *"FieldInfo has no attribute in_"* |

Fooocus ships `gradio==3.41.2` (2023) but leaves the FastAPI web stack unpinned;
on a 2026 package index pip pulls the latest, which is incompatible. The pins above
are the era-correct 2023 combo that matches gradio 3.41.2. They live in
`/srv/ai/Fooocus/constraints_v100.txt`.

**Install (reproducible).** Codified in
[`scripts/install-fooocus.sh`](../scripts/install-fooocus.sh):

```bash
/srv/ai/scripts/install-fooocus.sh
```

It clones to `/srv/ai/Fooocus`, creates `/srv/ai/venvs/fooocus`
(`--without-pip` + get-pip.py), installs the cu124 torch and the constrained
requirements, and verifies the `ldm_patched` backend imports on torch 2.6.

**Run it (port 7865).**

```bash
# one-off, foreground
cd /srv/ai/Fooocus && CUDA_DEVICE_ORDER=PCI_BUS_ID \
  /srv/ai/venvs/fooocus/bin/python launch.py --port 7865 --listen 127.0.0.1
```

> **Port gotcha:** `ldm_patched`'s default port is **8188 — the same as ComfyUI**.
> Always pass `--port 7865` (the service and one-liner above do). Omitting it
> collides with the running ComfyUI.

As a service (needs sudo):

```bash
sudo cp /srv/ai/scripts/fooocus.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fooocus
# logs: journalctl -u fooocus -f
```

The UI is then at `http://<server>:7865` and appears on the
[status page](server-setup.md) Services panel ("Fooocus").

**Models.** On the first real generation Fooocus downloads its default SDXL
checkpoint (JuggernautXL, ~7 GB) into `Fooocus/models/checkpoints/`, plus a few
small LoRAs/VAEs. Pass `--disable-preset-download` to skip that (e.g. a headless
smoke test). Sharing checkpoints/LoRAs with ComfyUI is a **later** step (see
ADR-0020) — for now Fooocus keeps its own model dirs.

**GPU / device.** The service sets `CUDA_DEVICE_ORDER=PCI_BUS_ID`. Fooocus otherwise
grabs the "fastest" visible card; with SDXL it comfortably fits a single V100
(32 GB) or the Titan X (12 GB). sm_70 has no fp8/FlashAttention-2, so Fooocus uses
PyTorch cross-attention (sdpa) here — the correct path for Volta.

**Attribution.** Fooocus © lllyasviel (Lvmin Zhang) and contributors, GPL-3.0 —
<https://github.com/lllyasviel/Fooocus>.

---

## SwarmUI, InvokeAI

Planned — will be documented here as each is installed (same pattern: own venv,
own port, cu124/torch-2.6 build, systemd unit, status-page entry).

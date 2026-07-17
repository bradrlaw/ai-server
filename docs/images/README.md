# Documentation images

Screenshots and diagrams referenced by the repo docs. Drop the files here with the
exact names below so the existing Markdown references resolve.

| File | What it should show |
|------|---------------------|
| `owui-status-banner.png` | The live status banner at the top of the Open WebUI new-chat screen (loaded models + host stats), pushed by `server-status-service.py`. |
| `server-status-page.png` | The host-side status dashboard served at `http://<host>:9095/` (models, ComfyUI queues, per-GPU util/VRAM/power/temp, host CPU/RAM/disk). |

Tips: crop to the relevant area, keep them reasonably sized (PNG, ideally < ~500 KB),
and avoid capturing anything sensitive (API keys, private prompts, personal chat text).

# ADR-0033: Qwen3.8-Flash-Next (Strata) as a swappable `flash` model in llama-swap

- **Status:** Accepted (implemented on branch `flash-production-integration`; nothing privileged needed)
- **Date:** 2026-10-25
- **Deciders:** @bradrlaw (+ Copilot CLI)
- **Related:** [ADR-0032](0032-qwen3.8-flash-next-strata-evaluation.md) (evaluation), [ADR-0015](0015-llama-swap-serving-modes.md) (modes),
  [ADR-0028](0028-host-firewall-ufw.md) (firewall)

## Decision
Expose Flash-Next (Strata, UD-Q4_K_XL, 512K context, GPU vision encoder) to every client through the existing path
LiteLLM `:4000` -> llama-swap `:9090`, as model `flash`:
- `config/llama-swap.base.yaml`: a `flash` entry (`scripts/strata-serve-port.sh config/strata/flash.json ${PORT}`,
  `checkEndpoint: /health`, `healthCheckTimeout: 600`, `ttl: 1800`), matrix var `fl`, set `fn: "fl & (f | fu)"`, evict cost 30.
  `flash` is exclusive with everything on idx1/idx2; `small` on idx0 coexists.
- `config/modes/flash.yaml`: a mode that keeps `flash` warm. `daily` restores the trio.
- `docker/litellm/config.yaml`: `flash` model_list entry. No firewall change (clients use `:4000`).
- `config/strata/flash.json`: the Strata config. The GPU vision encoder needs `strata-vision` built with
  `-DSTRATA_VISION_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=70` (see `docs/server-setup.md`).

## Verified (2026-10-25, live)
- Requesting `flash` in `daily` evicts coding+chat and loads Strata in about 90 s; `small` stays. Requesting `coding` evicts
  `flash` and reloads the trio (about 2 min). After `set daily` there are 0 Strata processes, 0 Xid and `verify-gpu-state.sh` is ALL GOOD.
- Text and image requests work through LiteLLM `:4000` (`flash` is listed in `/v1/models`).

## Consequences / watch
- **Any request to coding/chat/big evicts `flash`**, and vice versa (a 1.5-2 min swap). Pollers or agents that hit `coding`
  while `flash` is in use will bump it; use the `flash` mode and avoid mixing the two.
- `ttl: 1800` frees the V100s after 30 idle minutes but does not reload the trio; the next request for one does.
- Strata gives no batching gain (about 66 tok/s total for 4 clients) and was soaked for only 30 minutes.
- Roll back: delete the `flash` entry, var and set, the LiteLLM entry and `config/modes/flash.yaml`, re-run
  `llama-swap-mode.py set daily`, and restart `litellm`.

## Addendum 2026-10-25: Open WebUI info icon (speed stats) needs a local Strata patch
Open WebUI shows the per-answer info icon from `timings` (llama.cpp's field) merged with `usage`. Through LiteLLM, `flash` answers had
none. Root cause is a LiteLLM proxy bug: `_fast_serialize_simple_model_response_stream` (litellm/proxy/proxy_server.py) serializes
simple chunks without extra fields, and only falls back to the full serializer when the chunk has `system_fingerprint`, `usage` or
`provider_specific_fields`. llama.cpp always sends `system_fingerprint`, so its `timings` survive; Strata did not, so they were dropped.
Verified: adding `system_fingerprint` to Strata's chunks that carry `timings` is enough (stream shape unchanged).
`config/strata/openai-stream-timings.patch` (3 lines; `git apply` in `src/strata`, then reload `flash`) does that. `src/strata` is
gitignored, so **re-apply it after any Strata update**. The proper fix is upstream in LiteLLM (draft issue kept outside the repo).

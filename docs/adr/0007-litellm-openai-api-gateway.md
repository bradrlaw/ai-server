# ADR-0007: LiteLLM as OpenAI-style API gateway/router

- **Status:** Accepted (2026-07-02, see the update below)
- **Date:** 2026-07-01
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The long-term goal is to access this server only via **OpenAI-style API(s)**.
Multiple backends will run concurrently (P100 embeddings/TTS/STT, V100 coding
models), each potentially exposing its own OpenAI-compatible endpoint. Clients
want a single, stable entry point, and the user wants "agent routing" (route a
request to the right model/backend).

## Decision (proposed)
Place **LiteLLM** in front of the engines as a unified **OpenAI-compatible
gateway/router**: one endpoint, model-name-based routing to the appropriate
backend (llama-server on V100s, aux services on P100), plus key-based auth and
usage tracking. Front with a reverse proxy; run services under systemd.

## Update (2026-07-02): Accepted
Promoted to **Accepted** as part of the overall architecture (ADR-0010). LiteLLM is
the single OpenAI-compatible gateway; all access surfaces (Open WebUI, OpenClaw,
coding tools, autonomous agents) consume it. Per-user/tool **virtual keys with
budgets** double as autonomous-agent guardrails; usage/traces flow to Langfuse. Runs
in the containerized app tier (ADR-0006 hybrid). Backends: llama-swap (V100/P100
LLMs), P100 aux services (embeddings/STT/rerank), optional vLLM. Routing table lives
in `../architecture.md`.

## Consequences
- Positive: single OpenAI-style URL for all clients; centralizes auth, routing,
  logging; matches the "agent routing" goal; backend swaps stay transparent.
- Negative: an extra hop/component to run and monitor; must secure the gateway.
- Follow-up: confirm LiteLLM covers needed endpoints (chat, embeddings, audio);
  define the model->backend routing table. Promote to Accepted once validated.

## Alternatives considered
- **Expose each engine's endpoint directly** - rejected; no unified auth/routing,
  leaks topology to clients.
- **Custom proxy** - rejected for now; unnecessary when LiteLLM exists.

## Addendum (2026-10-15) — the gateway adapts tool schemas to llama.cpp's grammar limits

**Context.** llama.cpp compiles tool-parameter and `response_format` JSON schemas into a
sampling grammar, and one incompatible keyword anywhere 400s the whole request:
- An unanchored `pattern` fails conversion (`Pattern must start with '^' and end with '$'`).
  JSON Schema patterns are search patterns and are normally unanchored (TypeBox, zod, pydantic).
- Large `maxLength`/`maxItems` bounds compile to repetitions that exceed the grammar parser's
  2000-rule limit (`Failed to initialize samplers: failed to parse grammar`).

OpenClaw's owner-only `cron` tool has both (`declarationKey` `pattern "\S"`, `trigger.script`
`maxLength 65536`). OpenClaw only sends it to admin-scoped clients such as the Control UI, so
every Control UI chat failed on the Qwen slots (`chat`, `coding`), while CLI-driven runs worked.

**Decision.** The LiteLLM pre-call hook (`docker/litellm/custom_hooks.py`) removes these
keywords before forwarding:
- any `pattern` not written `^...$`;
- `minLength`/`maxLength` on string types, and `minItems`/`maxItems`, of 1000 or more.

Measured cutoffs on build 9850: strings fail from `maxLength` 2000, and arrays with `minItems` 0
from `maxItems` 1999 (the optional wrapper's rules count toward the limit). 1000 leaves margin
for converter changes. Each distinct drop is logged once. Every client passes through the
gateway, so one fix covers OpenClaw, Hermes, Open WebUI tools, Copilot BYOK and pi.

**Consequences.**
- Positive: client tool schemas no longer have to suit llama.cpp. Verified live with the real
  `cron` schema: `chat`, `coding` and `small` return 200 via `:4000`, and `chat`/`coding` emit
  valid `cron` calls; the same request direct to `:9090` still 400s.
- Negative: the constrained grammar is looser than the client's schema wherever a keyword was
  dropped. Dropping beats the alternatives: anchoring changes a pattern's meaning, and clamping
  a bound would cut long arguments short. OpenClaw re-validates tool calls against its own
  schema (`validateToolArguments`), so a violating call becomes a tool error the model can
  retry. Direct `:9090` callers aren't covered. Every backend here is llama.cpp; gate the
  rewrite per deployment if a hosted model is ever added.
- Not handled (never seen in tool schemas): a large `{m,n}` quantifier, or nested repetitions,
  inside an anchored pattern.
- Rejected: patching llama.cpp (a local patch to carry through every rebuild); per-client fixes
  (each covers only one client).

## Addendum (2026-10-15) — scoped secrets, non-root mcpo, one master key (audit B5)

**Context.** `litellm` and `mcpo` loaded all of `docker/.env` through `env_file`, so each held
all 8 secrets: the OpenClaw gateway token, the Hermes dashboard password and so on. mcpo was the
riskiest holder. It runs LLM-driven tools (fetch, git, plan-build) as root with all of `/srv/ai`
mounted read-only. Container root ignores file modes, so it could also read every brad-only 0600
secret file. Its own API key sat on its command line and in its healthcheck, where compose had
pasted the value in at parse time. Every host account can read both with `ps`.

**Decision.**
- Each service gets only the `.env` keys it uses, through compose `environment:` interpolation.
  No `env_file: ./.env`. LiteLLM gets `LITELLM_MASTER_KEY`. mcpo gets `MCPO_API_KEY` plus
  `LITELLM_MASTER_KEY` for plan-build. The other services already listed their keys explicitly.
- mcpo runs as uid/gid 10001 (no host account) with `no-new-privileges`, since the image ships
  setuid `su` and `mount`. `HOME` is a new `mcpo-home` volume that holds the uv cache, which keeps
  the 2026-07-26 boot-race guard. A one-shot `mcpo-init` (root, no network) chowns it. The old
  root-owned `mcpo-uv-cache` volume is deleted.
- The API key reaches mcpo through its environment. mcpo's `--api-key` has no env fallback, so
  the entrypoint is a python one-liner that adds the key to `sys.argv` inside the process. The
  healthcheck uses `$$MCPO_API_KEY`, which the container shell expands at check time; `printf`
  is a builtin and curl reads the header from stdin (`-H @-`), so no argv holds the key.
- **Keep one master key for now (owner decision).** The 2026-07-02 update planned per-client
  virtual keys with budgets. They need LiteLLM's Postgres-backed key store, which this deployment
  doesn't run. One key serves Open WebUI, OpenClaw, Hermes, aichat, pi, Copilot BYOK, the
  eval/bench scripts, and plan-build (in mcpo and in the host `plan-build-mcp.service`). Todo
  `litellm-virtual-keys` tracks the revisit.

**Found while testing, and fixed.** plan-build ran `uv run --with mcp` in mcpo's working dir
`/app`, which is mcpo's own uv project. So on every start uv re-locked `/app/uv.lock` and
re-synced mcpo's venv to it. As root this silently worked: the live venv held mcp 1.17.0
instead of the shipped 1.26.0, older fastapi/pydantic/starlette/uvicorn/typer, and pytest, all
swapped in under the running server. A `docker restart` keeps that layer and would have booted
mcpo on those versions. As uid 10001 the sync fails and plan-build doesn't start. Running
`uv run --no-project` fixes both: the script runs on the image's own env, which already has
`mcp`, and `/app` stays untouched. The `UV_CACHE_DIR=/tmp/uv-cache` override is gone too.

**Verification (live 2026-10-15).** First, a throwaway uid-10001 container passed with the new
settings, including an offline start from the cache (`--network none`). After
`docker compose up -d mcpo-init mcpo litellm`:
- Both containers were healthy in about 24 s, and all 7 MCP servers connected, including
  `comfyui-secure` over `mcp-secure`.
- `time`, `git_log`, `fetch`, `current_mode` and `comfyui-secure/get_defaults` return 200.
  A request with no key gets 401.
- Every mcpo process runs as uid 10001 with NoNewPrivs set. No `.env` secret appears in any
  host command line (before: 1) or in the container config.
- mcpo can no longer read `docker/.env`, either ComfyUI-Login PASSWORD or `.secret-key.txt`,
  `hermes/config.yaml`, `var/status-service/auth.json` or `scripts/server-status.env`.
- `/app` has no changes.
- LiteLLM lists 9 models and answers a `small` chat. Open WebUI and OpenClaw reach it with
  their keys.

**Residuals.**
- mcpo hands its whole environment to every stdio child, and the `uvx` servers are unpinned
  PyPI packages. A compromised package would get the master key. That is the main argument for
  virtual keys.
- uid 10001 can still read world-readable files under `/srv/ai`, such as the 0775 private
  ComfyUI output dirs.
- The host `plan-build-mcp.service` loads the whole `.env` through `EnvironmentFile`. It runs as
  brad, who owns the file, so no boundary is crossed.

## Addendum (2026-10-17) — scoped autoheal socket access, pinned images (audit B7)

**Finding (LOW).** `autoheal` mounted `/var/run/docker.sock:ro`. The `:ro` bind flag only
makes the *file* read-only to the container; the Docker socket itself has no read-only
concept, so a compromised `autoheal` process could issue any Docker Engine API call — exec
into another container, start a new one with the host root bind-mounted, etc. — effectively
root on the host. Separately, four images were unpinned: `autoheal:latest`, `searxng:latest`,
`hermes-agent:latest`, `mcpo:main`, so a routine `docker compose pull` could silently change
what's running.

**Fix.**
- Added `docker-socket-proxy` (`ghcr.io/tecnativa/docker-socket-proxy`, pinned by digest): the
  only container that touches the real socket. It forwards just `CONTAINERS` (list/inspect, for
  health status), `EVENTS` (autoheal's event stream) and `ALLOW_RESTARTS`+`POST` (the one
  mutating call autoheal needs); everything else — `exec`, `images`, `volumes`, `auth`,
  `secrets`, `swarm`, container `create`/`run`, etc. — answers `403 Forbidden`. It sits on a new
  `internal: true` network (`docker-proxy`) with nothing attached but itself and `autoheal`.
- `autoheal` now talks to `tcp://docker-socket-proxy:2375` via `DOCKER_HOST` + `DOCKER_SOCK`
  (the image needs both set to use TCP) instead of the raw socket; no socket mount at all.
- Pinned `autoheal`, `searxng`, `hermes-agent`, `mcpo` (both `mcpo-init` and `mcpo`, which must
  share a digest since `mcpo-init` chowns for that exact image's uid) to the digest that was
  running at audit time (`docker image inspect <tag> --format '{{.RepoDigests}}'`), with a
  comment at each explaining how to intentionally re-pin after a deliberate update. None of the
  `:latest`/`:main` tags matched a numbered release tag at the time (e.g. `autoheal:latest`'s
  digest didn't match `1.2.0`, the newest numbered tag), so pinning to a "stable" version tag
  would have been an unreviewed downgrade; pinning to the currently-deployed digest freezes
  exactly what's already running and tested.

**Verified (live 2026-10-17).** `docker compose up -d`: all 8 containers healthy/running.
From inside `autoheal`, `GET docker-socket-proxy:2375/containers/json` returns the container
list (200); `GET .../images/json` returns `403 Forbidden`. `autoheal` logs show it reached
"Monitoring containers for unhealthy status" (no socket errors). `mcpo` and `searxng` still
serve traffic on their usual ports.

**Residual.** `docker-socket-proxy` itself still runs `--privileged` (upstream requirement to
reach the Unix socket under some LSM policies) and mounts the real socket read-only — it is the
one component that must hold that access; the fix narrows *who else* can reach the Docker API
through it, not the proxy's own access to the daemon.

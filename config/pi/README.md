# pi.dev coding harness → AI server (LiteLLM)

Config to drive the AI server's local models from the [pi.dev](https://pi.dev)
coding agent over its OpenAI-compatible gateway (LiteLLM on `:4000`).

pi runs on **your Mac** (the client); it talks to LiteLLM on the server. Nothing
about pi is installed on the server itself.

## 1. Install pi on the Mac

```sh
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi --version
```

(`--ignore-scripts` is recommended by pi's own install docs.)

## 2. Drop in the provider config

Copy `models.json` from this repo to `~/.pi/agent/models.json`:

```sh
mkdir -p ~/.pi/agent
cp config/pi/models.json ~/.pi/agent/models.json
```

Then edit the `baseUrl` to point at the server as your Mac reaches it — the LAN
IP or the Tailscale address (**not** `localhost`, since pi runs on the Mac):

```json
"baseUrl": "http://192.168.4.57:4000/v1"      // LAN example
"baseUrl": "http://<tailscale-ip>:4000/v1"    // Tailscale example
```

`models.json` reloads every time you open `/model` — no restart needed.

## 3. Provide the LiteLLM key

The config reads the key from the environment via `"$LITELLM_MASTER_KEY"`.
Export it in the shell you launch pi from (get the value from the server's
`docker/.env`, key `LITELLM_MASTER_KEY`):

```sh
export LITELLM_MASTER_KEY=sk-...        # value from server docker/.env
pi
```

Alternatives to the env var:
- `pi --api-key sk-... --model coding`
- store it once with `/login` (pi writes `~/.pi/agent/auth.json`).

## 4. Use it

```sh
pi --model coding -p "Summarize this repository"      # one-shot
pi --model chat                                       # interactive
```

In interactive mode, `/model` lists the `aiserver` models below.

## Models

| pi model     | Backing model              | GPU          | Notes                                  |
|--------------|----------------------------|--------------|----------------------------------------|
| `coding`     | Qwen3.6-27B dense          | V100 idx1    | best quality, slowest, reasoning       |
| `chat`       | Qwen3.6-35B-A3B MoE        | V100 idx2    | near-coding quality, much faster, reasoning |
| `coder-next` | Qwen3-Coder-Next 80B-A3B   | both V100s   | non-thinking, preempts coding+chat     |
| `fast`       | Gemma-4-12B                | P100 idx0    | always warm, non-reasoning             |

Add more from the roster (`big`, `gemma-31b`, `chat-uncensored-q6`, …) by copying
a model block and changing `id` (must match a LiteLLM model id exactly — ids are
**lowercase and case-sensitive**).

## Compatibility notes (why the `compat` flags)

The models are served by llama.cpp behind LiteLLM. llama.cpp's OpenAI endpoint
does **not** understand the `developer` role or `reasoning_effort`, so the provider
sets:

```json
"compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false }
```

Without these, reasoning-capable models (`coding`, `chat`) error out. The system
prompt is sent as a `system` message instead.

Reasoning models run a thinking phase, so give them generous `maxTokens`
(already set to 32768 here). `coder-next` is non-thinking (direct code).

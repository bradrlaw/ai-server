# aichat — terminal LLM client + comfyui-secure MCP tools

Lightweight terminal chat client for this server, pointed at the LiteLLM gateway
(`:4000`), with the **comfyui-secure** image/media MCP tools wired in so you can
generate images from the shell. Default model is the uncensored chat slot; plain
"generate an image …" requests route to the local **krea2 turbo bf16** workflow.

Everything the client *runs from* is **local & gitignored** (`~/.local/bin`,
`~/.config/aichat`, `/srv/ai/tools/llm-functions`). This directory holds the
**tracked, reproducible snapshot**: config template, role, the MCP bridge patch,
and the MCP server list. See `docs/adr/0025-aichat-terminal-client-mcp.md` and the
"aichat terminal client" section of `docs/server-setup.md` for the full runbook.

## Upstream open-source components (all MIT, by sigoden)

| Component | Repo | License | Role |
|-----------|------|---------|------|
| `aichat` v0.30.0 | https://github.com/sigoden/aichat | MIT | the terminal client (static musl binary in `~/.local/bin`) |
| `argc` v1.24.0 | https://github.com/sigoden/argc | MIT | builds `llm-functions` tool shims + `functions.json` |
| `llm-functions` @ `616d8d8` | https://github.com/sigoden/llm-functions | MIT | function-calling runtime + MCP bridge (`mcp/bridge/index.js`) |

We **patched** `llm-functions/mcp/bridge/index.js` (see
`llm-functions-bridge.patch`, +77/−8). The upstream bridge only spoke stdio,
passed MCP schemas through verbatim, and listened on every interface. Our changes,
MIT-licensed like the original:

1. **HTTP transports** — connect to a URL-based MCP server via
   `StreamableHTTPClientTransport` (or `SSEClientTransport` when `type: "sse"`),
   in addition to the upstream stdio transport. Needed because comfyui-secure's
   MCP is streamable-http at `http://127.0.0.1:9001/mcp`.
2. **`repairSchema`** — name-based type inference for params the comfyui-secure
   schema leaves typeless (`width`/`height`/`seed`/`steps` → integer,
   `cfg`/`denoise`/`strength` → number; typeless fallback → string). Without this,
   aichat's strict parser rejects the schema or the model mirrors the param name
   into the value (`{"width":{"width":1024}}`) and loops.
3. **`sanitizeArgs`** — unwraps a double-wrapped `{"width":{"width":N}}` →`N`
   while preserving genuine object params (e.g. `param_overrides`).
4. **Loopback bind.** The bridge listens on `MCP_BRIDGE_HOST`, default
   `127.0.0.1`, instead of upstream's all-interfaces `app.listen(PORT)`. It has
   no auth and holds a live session to the login-gated comfyui-secure MCP, so it
   must never be LAN-reachable. The systemd unit also kernel-filters it to
   localhost. See `docs/adr/0026-secure-mcp-bridge-network-lockdown.md`.

Open WebUI is unaffected: it reaches the same comfyui-secure MCP through **mcpo**
(typed OpenAPI), a completely separate path. Only this aichat bridge is patched.

## Files here

- `config.example.yaml` — aichat config template. Copy to `~/.config/aichat/config.yaml`
  (perms `600`) and replace `sk-REPLACE_WITH_LITELLM_MASTER_KEY` with the real
  `LITELLM_MASTER_KEY` from `docker/.env`. **Never commit the real key.**
- `roles/default.md` — the default role. Its frontmatter `use_tools: comfyui` is
  what actually activates the tools (a top-level `use_tools` in config is ignored
  by aichat); the body steers unqualified image requests to `krea2_turbo_bf16`.
- `llm-functions-mcp.json` — copy to `/srv/ai/tools/llm-functions/mcp.json`; lists
  the comfyui-secure MCP server the bridge connects to.
- `llm-functions-bridge.patch` — apply inside the `llm-functions` clone with
  `git apply` (or `patch -p1`) after cloning upstream `616d8d8`.

## Two gotchas that cause silent "no image, fake URL" failures

- **Tools must be activated by the role, not the top-level config.** aichat only
  attaches tools when activated via `--use-tools`, the REPL `.use-tools`, or a
  role/agent frontmatter. `roles/default.md` carries `use_tools: comfyui`; the
  `cmd_prelude`/`repl_prelude: role:default` in config apply it to every session.
  If tools aren't attached, the model invents a `<tool_call>` as plain text **and
  hallucinates a result URL** (we saw a bogus `s3.amazonaws.com` link) — nothing
  is actually generated and nothing leaves the box.
- **Each tool-using model needs `supports_function_calling: true`** in its config
  entry, or aichat won't send tools to it.

## Disabling the per-tool confirmation prompt

`/srv/ai/tools/llm-functions/.env` (gitignored — it's a `*.env`) sets:

```
LLM_MCP_SKIP_CONFIRM=comfyui_secure_[a-z0-9_]+
```

This makes `scripts/run-mcp-tool.sh` skip its interactive
`Are you sure you want to continue? [Y/n]` prompt for the comfyui-secure tools
(regex is `grep -w -E` matched against the full tool name — the `0-9` matters for
names like `krea2_turbo_bf16`). Scoped to `comfyui_secure_*` on purpose so any
other MCP server added later still prompts by default. Use `.+` to skip all.

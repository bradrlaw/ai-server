# ADR-0027: Sign-in + CSRF protection for the status dashboard (:9095)

- **Status:** Accepted
- **Date:** 2026-10-13
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The `server-status` service (`scripts/server-status-service.py`, `:9095`, binds
`0.0.0.0`, runs as `brad`) serves the HTML dashboard and its `POST /actions/*`
endpoints. Those endpoints:

- start/stop the ComfyUI instances and the managed creative tools (via the scoped
  sudoers rule);
- set GPU power caps, up to the card maximum;
- toggle `--fp16-unet`;
- pause quiet hours;
- sync ComfyUI workflows into the MCP bridges.

The 2026-10-13 repo audit (finding B3) found:

- **No authentication** on any of them. Any LAN or tailnet device could call them.
- **`Access-Control-Allow-Origin: *`** on every response. Combined with the first
  point, any web page the owner happened to visit could drive them through the
  owner's browser. A cross-site `fetch` POST without custom headers is a "simple"
  request that browsers send without a preflight. The wildcard also let any page
  read the status JSON.

ufw is off (B2). It wouldn't stop the CSRF case anyway, because that request comes
from the owner's own browser on the LAN.

Consumers, inventoried before the change:

- the dashboard's own JS (same origin);
- Open WebUI's global "Server Status" inlet filter, which does
  `GET http://host.docker.internal:9095/status.json` from the `open-webui`
  container, with no credentials;
- `curl 127.0.0.1:9095/status.json` / `history.json` examples in the docs.

Nothing scripted POSTs to `/actions/*`, and there is no reverse proxy or
`tailscale serve` in front of the page.

**Owner's decision:** require a sign-in for each browser that connects, and block
cross-site requests.

## Decision
All of this is in the service itself, using the Python stdlib only (no new
dependencies).

1. **One shared dashboard password, per-browser sessions.**
   - **Setting it.** Run `python3 scripts/server-status-service.py --set-password`
     as the service user. It needs no sudo and no restart.
     - It refuses to run as root, needs at least 8 characters, and asks twice.
     - The password is stored as PBKDF2-HMAC-SHA256 (600k iterations, random
       salt) in `/srv/ai/var/status-service/auth.json`, written atomically with
       mode 0600.
     - The installer prompts for a password if none is set.
   - **Fail closed.** If there's no password file, or it can't be read, nobody
     can sign in.
   - **Live password changes.** The file is re-read whenever it changes. Each
     session is tied to the hash it was created under, so changing the password
     signs every browser out.
   - **Sign-in.** `POST /login` (HTML form) returns 303 with
     `Set-Cookie: ai_status_sid=<256-bit random>; Path=/; HttpOnly; SameSite=Strict`,
     a browser-session cookie.
     - The cookie name is unique because cookies ignore ports, so this host's
       other apps share the browser's cookie jar.
     - Sessions live in memory: 12 h idle timeout, 7 d hard cap, at most 64
       (oldest evicted first).
     - **Sign out** drops the session.
   - **Brute force.**
     - Each failed sign-in waits 1 s before answering.
     - 5 failures from one source IP within 15 min get 429 responses until the
       window passes.
     - Successful and failed sign-ins (and blocked cross-site ones) are logged to
       the journal as `[auth] …` with the source IP.
2. **Every state-changing request needs three things,** from every source
   address, loopback included:
   - **a valid session;**
   - **a same-origin request:**
     - `Sec-Fetch-Site`, when present, must be `same-origin` or `none`;
     - `Origin`, when present, must equal `http(s)://<Host>` or an entry in
       `STATUS_EXTRA_ORIGINS` (reserved for a future TLS proxy).

     The other services on this host (Open WebUI, ComfyUI, Filebrowser, …) are
     different **origins** but the same **site**. That means `SameSite=Strict`
     cookies still flow from their pages to `:9095`, so the cookie alone isn't
     enough. `POST /login` gets the same check, to stop login CSRF.
   - **the session's CSRF token in an `X-CSRF-Token` header.** The token is
     rendered into the page as `<meta name="csrf-token">`, sent by the
     dashboard's `api()` fetch wrapper, and compared in constant time.

   A missing session gets 401 (the page sends you to sign in). A cross-site
   request or a bad token gets 403.
3. **No CORS, plus hardening headers.**
   - The `Access-Control-Allow-Origin` header is gone.
   - There is no `OPTIONS` handler, so a cross-origin request that carries the
     custom header fails its preflight and is never sent.
   - Every response also sends:
     - `Cache-Control: no-store`;
     - `X-Content-Type-Options: nosniff`;
     - `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'` (against
       clickjacking);
     - `Referrer-Policy: same-origin`;
     - `Cross-Origin-Resource-Policy: same-origin`.
   - Request bodies are capped at 8 KiB.
4. **Trusted read-only exemption.**
   - **What is exempt:** `GET` of `/status.json`, `/history.json` (and their
     extension-less aliases) and the benchmark chart PNGs.
   - **Who can read them without signing in:** the sources in
     `STATUS_TRUSTED_READ_NETS`. The default is loopback plus `172.16.0.0/12`,
     i.e. the host itself and Docker containers. This keeps the Open WebUI banner
     and local `curl` working.
   - **What stays locked:** the exemption never covers `/`, `/login` or any POST.
     From the LAN or Tailscale, these reads need a session, just like the page.
   - `/healthz` stays public.

## Consequences
- **Positive:**
  - LAN and tailnet devices can no longer start/stop services, raise GPU power
    caps, toggle fp16 or pause quiet hours without the password.
  - Other web pages, including the other apps on this host, can no longer drive or
    read the dashboard through the owner's browser.
  - No client changes. The Open WebUI filter and the loopback `curl` examples keep
    working, and the dashboard JS sends the token itself.
  - Password changes apply live, revoke every session, and need no sudo.
- **Negative / trade-offs:**
  - **Plain HTTP.** The password and the session cookie cross the LAN in clear
    text (Tailscale traffic is WireGuard-encrypted). This is the same posture as
    ComfyUI-Login and Open WebUI on this box. Use a password that isn't reused
    anywhere else.
  - **Containers can read the status JSON.** Any container, including the
    Hermes/OpenClaw agents, and anything on the host can read GPU, model and
    service state, quiet-hours state and power caps.
    - This is read-only and low-sensitivity.
    - If that changes, narrow `STATUS_TRUSTED_READ_NETS` to loopback. The Open
      WebUI banner would then stop working unless it gets its own credential.
  - **Actions can't be scripted with plain `curl`** (they need a session and the
    token). Automation would need its own, deliberately designed path.
  - **Restarts sign everyone out.** Sessions are in memory, so restarting
    `server-status` means signing in again.
  - **One shared password.** There is no per-user identity; the journal only
    records source IPs.
- **Follow-ups / things to watch:**
  - ufw (audit B2) is complementary: it limits who can reach `:9095` at all.
  - If the page is ever served over HTTPS (e.g. `tailscale serve`):
    - add `Secure` to the cookie;
    - list the proxy's origin in `STATUS_EXTRA_ORIGINS`;
    - note that the proxy becomes a loopback client, so it would fall under the
      trusted-read exemption for the JSON endpoints.

## Alternatives considered
- **Loopback bind + SSH tunnel only:** the strongest option. Rejected because the
  owner uses the dashboard from other LAN/tailnet devices, and the Open WebUI
  filter needs container access.
- **HTTP Basic auth:** no real sign-out (browsers cache it until restart), and
  browsers attach it to cross-site requests automatically, so CSRF protection would
  still be needed. Rejected.
- **A shared API key in the URL or page:** it leaks via history and bookmarks, and
  it isn't a sign-in. Rejected.
- **ufw / IP allowlist only:** stops other devices, but not CSRF from the owner's
  own browser. Not sufficient on its own; complementary (B2).
- **Authenticating reverse proxy** (Caddy/nginx/`tailscale serve` with TLS): the
  best long-term transport, but it adds another service and certificates to run.
  The in-app sign-in is self-contained and can be layered behind a proxy later
  (`STATUS_EXTRA_ORIGINS`).
- **`SameSite` cookies alone:** not enough, because the other ports on this host
  are the same site.

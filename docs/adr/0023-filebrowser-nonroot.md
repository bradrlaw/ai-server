# ADR-0023: Run the Filebrowser container as non-root (1000:1000)

- **Status:** Accepted
- **Date:** 2026-09-13
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The `filebrowser` service (`docker/docker-compose.yml`, host `:8083`) bind-mounts
several host trees **read-write** — including the full ComfyUI models tree
(`/srv/ai/comfyui/models`) and both instances' `output-*`/`input-*` dirs — so the
owner can upload/manage models and media from a browser.

The service had **no `user:` directive**, so the container ran as **root (uid 0)**.
Docker bind mounts preserve the process's *numeric* UID onto the host, so every
file uploaded through the Filebrowser UI was written to the host as **root:root**.
Over months this left ~60 root-owned model files under `comfyui/models/` (and one
under `output-open/`) that `brad` cannot modify or delete — including over the new
SMB share (ADR-0022). More importantly, a root-in-container process with RW host
mounts is an unnecessary privilege: a Filebrowser vulnerability or misconfig could
write anywhere in the mounted trees as root.

(For reference, the same bind-mount-UID mechanism is why `docker/searxng/settings.yml`
shows as uid `977` — the SearXNG image's internal user. That one is intentional and
left as-is; the container must read its own config.)

## Decision
**Run Filebrowser as `brad` (`user: "1000:1000"`)** instead of root, and drop the
ability to regain privileges.

Changes to the `filebrowser` service:
- `user: "1000:1000"` — uploads now land on the host owned by `brad:brad`.
- `security_opt: [no-new-privileges:true]` — defense-in-depth.
- Move the in-container listener off privileged port 80 (a non-root process can't
  bind <1024): `FB_PORT: "8080"` and port mapping `8083:8080` (host port `:8083`
  is unchanged, so nothing downstream moves).

Operational migration (owner sudo — agents cannot):
1. Reclaim existing root-owned files so `brad` can manage them:
   `sudo chown -R brad:brad /srv/ai/comfyui/models /srv/ai/comfyui/output-open`
2. Make Filebrowser's own config DB (named volume `filebrowser-data`) readable by
   uid 1000, or it will crash on start:
   `sudo chown -R 1000:1000 /var/lib/docker/volumes/filebrowser-data/_data`
3. Recreate the container: `docker compose up -d filebrowser`.

## Consequences
- Positive: UI uploads are `brad`-owned and manageable (incl. over SMB); the
  container no longer runs as root with RW host mounts; `no-new-privileges` set.
- Neutral: host access port `:8083` unchanged; only the container-internal port
  moved to 8080.
- Caveats:
  - The `filebrowser-data` volume must be chowned to 1000 (step 2) before the
    container is recreated, else Filebrowser can't open its DB.
  - Existing SearXNG `settings.yml` (uid 977) is intentionally left alone.
  - Any other container that bind-mounts host paths RW should be audited for the
    same root-ownership pattern.

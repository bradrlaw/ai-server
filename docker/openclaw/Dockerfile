# Extends the pinned upstream OpenClaw image with the few CLI binaries that a
# handful of bundled skills need, so they don't sit in a permanent "needs setup"
# error state. The upstream image is Debian 12 (bookworm) and runs as user
# `node` (uid 1000); we drop to root only to apt-install, then restore `node`.
#
# Baked tools -> skills unblocked:
#   ffmpeg           -> video-frames
#   jq + ripgrep     -> session-logs (needs BOTH jq and rg)
#   gh (GitHub CLI)  -> github, gh-issues
#
# Node is overridden with a patched same-major (24.x, ABI 137) build from the official
# image, so a Node security fix doesn't force an OpenClaw upgrade: 2026.7.1 ships 24.16.0,
# hit by the Brotli decompression bug CVE-2026-111497 (fixed in 24.22.1). Bump NODE_IMAGE
# for Node fixes; when bumping BASE_IMAGE, drop the override if upstream's Node is newer.
#
# Bump BASE_IMAGE in lockstep with docs/server-setup.md when updating OpenClaw.
ARG BASE_IMAGE=ghcr.io/openclaw/openclaw:2026.7.1
ARG NODE_IMAGE=node:24.22.3-bookworm-slim
FROM ${NODE_IMAGE} AS node
FROM ${BASE_IMAGE}

USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
ENV NODE_VERSION=24.22.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      jq \
      ripgrep \
      ca-certificates \
      curl \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

USER node

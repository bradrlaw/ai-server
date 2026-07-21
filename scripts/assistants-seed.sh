#!/usr/bin/env bash
# Seed the OpenClaw + Hermes assistant-gateway runtime dirs (gitignored, under
# /srv/ai/{openclaw,hermes}) from the tracked templates in docker/{openclaw,hermes}.
# Idempotent: never overwrites an existing live config (so schema migrations /
# agent-written state persist). Run once before `docker compose up -d openclaw hermes`.
set -euo pipefail

REPO="/srv/ai"
DOCKER_DIR="$REPO/docker"
OC_DIR="$REPO/openclaw"
HM_DIR="$REPO/hermes"

# --- OpenClaw: three persistent dirs (state / workspace / auth-secrets) ---
mkdir -p "$OC_DIR/state" "$OC_DIR/workspace" "$OC_DIR/auth-secrets"
if [[ ! -f "$OC_DIR/state/openclaw.json" ]]; then
  cp "$DOCKER_DIR/openclaw/openclaw.json" "$OC_DIR/state/openclaw.json"
  echo "seeded $OC_DIR/state/openclaw.json"
else
  echo "kept existing $OC_DIR/state/openclaw.json"
fi

# --- Hermes: single /opt/data dir; inject the LiteLLM key into the live config ---
mkdir -p "$HM_DIR"
if [[ ! -f "$HM_DIR/config.yaml" ]]; then
  # Pull the master key from docker/.env (never printed).
  KEY="$(grep -E '^LITELLM_MASTER_KEY=' "$DOCKER_DIR/.env" | head -1 | cut -d= -f2-)"
  if [[ -z "${KEY:-}" ]]; then
    echo "ERROR: LITELLM_MASTER_KEY not found in $DOCKER_DIR/.env" >&2
    exit 1
  fi
  sed "s|REPLACE_WITH_LITELLM_MASTER_KEY|${KEY}|" \
    "$DOCKER_DIR/hermes/config.yaml" > "$HM_DIR/config.yaml"
  echo "seeded $HM_DIR/config.yaml (key injected)"
else
  echo "kept existing $HM_DIR/config.yaml"
fi

# Both apps run as uid/gid 1000 inside the container; match on the host.
chown -R 1000:1000 "$OC_DIR" "$HM_DIR" 2>/dev/null || true
echo "done."

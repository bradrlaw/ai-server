#!/usr/bin/env bash
# Prune orphaned Open WebUI uploads by running openwebui-prune-uploads.py INSIDE
# the open-webui container (where /app/backend/data paths are valid). The Python
# script is piped over stdin, so nothing needs to be copied into the image.
#
# Usage:
#   openwebui-prune-uploads.sh                 # dry-run (safe; shows what it would delete)
#   openwebui-prune-uploads.sh --apply         # actually delete orphans older than 7d
#   openwebui-prune-uploads.sh --apply --min-age-days 3
#
# The systemd service (openwebui-prune-uploads.service) calls this with --apply.
set -euo pipefail

CONTAINER="${OPENWEBUI_CONTAINER:-open-webui}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${SCRIPT_DIR}/openwebui-prune-uploads.py"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "openwebui-prune-uploads: container '$CONTAINER' is not running; skipping." >&2
  exit 0
fi

exec docker exec -i "$CONTAINER" python3 - "$@" < "$PY"

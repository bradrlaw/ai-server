#!/usr/bin/env bash
# Snapshot the ComfyUI environment so custom-node-pack installs can be undone.
#
# Captures three layers into <comfyui>/backups/ and the ComfyUI-Manager
# snapshots dir:
#   1. pip freeze of the shared venv   -> exact package versions (real undo for
#      the #1 risk: a node pack upgrading numpy/torch/etc in the shared venv)
#   2. custom_nodes git HEADs          -> which node dirs existed + their commit
#   3. ComfyUI-Manager snapshot JSON   -> core commit + all nodes + pip, restorable
#
# Usage:
#   scripts/comfyui-snapshot.sh                 # take a snapshot
#   scripts/comfyui-snapshot.sh --list          # list snapshots
#
# Restore (undo):
#   - venv:  <venv>/bin/pip install -r <comfyui>/backups/venv-freeze-<STAMP>.txt
#   - nodes: remove any custom_nodes/ dir not in custom-nodes-hashes-<STAMP>.txt,
#            or use the Manager UI "Snapshot Manager" -> Restore, or:
#            <venv>/bin/python custom_nodes/comfyui-manager/cm-cli.py \
#                restore-snapshot <STAMP>_snapshot.json
set -euo pipefail

COMFYUI="${COMFYUI_PATH:-/srv/ai/comfyui}"
VENV="${COMFYUI_VENV:-/srv/ai/venvs/comfyui}"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
BACKUPS="$COMFYUI/backups"

if [[ "${1:-}" == "--list" ]]; then
  echo "== pip freezes =="; ls -1t "$BACKUPS"/venv-freeze-*.txt 2>/dev/null || echo "  (none)"
  echo "== node hashes =="; ls -1t "$BACKUPS"/custom-nodes-hashes-*.txt 2>/dev/null || echo "  (none)"
  echo "== manager snapshots =="; ls -1t "$COMFYUI"/user/__manager/snapshots/*.json 2>/dev/null || echo "  (none)"
  exit 0
fi

mkdir -p "$BACKUPS"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "[1/3] pip freeze -> backups/venv-freeze-$STAMP.txt"
"$PIP" freeze > "$BACKUPS/venv-freeze-$STAMP.txt"
echo "      $(wc -l < "$BACKUPS/venv-freeze-$STAMP.txt") packages"

echo "[2/3] custom_nodes git HEADs -> backups/custom-nodes-hashes-$STAMP.txt"
: > "$BACKUPS/custom-nodes-hashes-$STAMP.txt"
for d in "$COMFYUI"/custom_nodes/*/; do
  [[ -e "$d/.git" ]] || continue
  printf '%s %s\n' "$(git -C "$d" rev-parse HEAD 2>/dev/null || echo '?')" "$(basename "$d")" \
    >> "$BACKUPS/custom-nodes-hashes-$STAMP.txt"
done
# also record the full dir listing (registry/file nodes have no .git)
ls -1 "$COMFYUI"/custom_nodes >> "$BACKUPS/custom-nodes-hashes-$STAMP.txt"

echo "[3/3] ComfyUI-Manager snapshot"
COMFYUI_PATH="$COMFYUI" "$PY" "$COMFYUI/custom_nodes/comfyui-manager/cm-cli.py" save-snapshot 2>&1 \
  | grep -Ei 'snapshot is saved|Current snapshot' || true

echo "Done. Snapshot stamp: $STAMP"

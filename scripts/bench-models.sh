#!/usr/bin/env bash
# Re-run llama.cpp benchmarks for any subset of the served models.
#
# The model registry (name -> gguf, GPU pinning, split mode) is read straight
# from config/llama-swap.yaml, so it always matches what the router actually
# serves — no hardcoded paths to drift. Uses llama-bench; no sudo required.
#
# Hardware (see docs/server-setup.md): nvidia-smi PCI_BUS_ID order is
# 0=P100, 1=V100, 2=V100. The V100s have NO NVLink, so a dual-card split
# crosses PCIe gen3 (PHB). CUDA_DEVICE_ORDER=PCI_BUS_ID is exported so the
# device indices below match nvidia-smi.
#
# Usage:
#   scripts/bench-models.sh                       # bench the daily set (coding chat fast)
#   scripts/bench-models.sh coding chat           # bench specific models by name
#   scripts/bench-models.sh --all                 # bench every model in the config
#   scripts/bench-models.sh --list                # list available model names + pinning
#   scripts/bench-models.sh --free coding         # unload llama-swap models first (avoid OOM)
#
# Options:
#   --all                 bench all models in the config
#   --list                print the registry and exit
#   --free                unload all llama-swap models before benching (no sudo; via API)
#   -p, --prompt N        prompt-processing tokens         (default 512)
#   -n, --ngen N          tokens to generate               (default 128)
#   -r, --reps N          repetitions                      (default 3)
#   -d, --depths "A B"    context depths to test           (default "0 8192")
#   -o, --out DIR         output directory                 (default models/bench-<stamp>)
#
# Note: llama-bench loads the model directly on its pinned GPU(s). If llama-swap
# already has a model resident there you may OOM — pass --free to unload first,
# or run when the card is idle.
set -euo pipefail

CONFIG="${LLAMA_SWAP_CONFIG:-/srv/ai/config/llama-swap.yaml}"
BIN="${LLAMA_BENCH_BIN:-/srv/ai/src/llama.cpp/build/bin/llama-bench}"
LLAMASWAP_URL="${LLAMASWAP_URL:-http://127.0.0.1:9090}"
MODELS_ROOT="${MODELS_ROOT:-/srv/ai/models}"
DAILY=(coding chat fast)

PROMPT=512
NGEN=128
REPS=3
DEPTHS_STR="0 8192"
OUT_DIR=""
DO_ALL=0
DO_LIST=0
DO_FREE=0
REQUESTED=()

log() { echo -e "\n\033[1;36m==> $*\033[0m"; }
die() { echo "error: $*" >&2; exit 1; }

# --- registry: emit "name<TAB>gguf<TAB>dev<TAB>sm" from llama-swap.yaml --------
registry() {
  python3 - "$CONFIG" "$MODELS_ROOT" <<'PY'
import sys, re, yaml
cfg, models_root = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open(cfg)) or {}
macros = d.get("macros", {}) or {}
mdir = macros.get("models_dir", models_root)
for name, m in (d.get("models") or {}).items():
    if not isinstance(m, dict):
        continue
    cmd = m.get("cmd", "") or ""
    env = " ".join(m.get("env", []) or [])
    dev = re.search(r"CUDA_VISIBLE_DEVICES=(\S+)", env)
    model = re.search(r"--model\s+(\S+)", cmd)
    sm = re.search(r"--split-mode\s+(\S+)", cmd)
    if not model:
        continue
    gguf = model.group(1).replace("${models_dir}", mdir)
    dev = dev.group(1) if dev else ""          # empty => all visible GPUs
    sm = sm.group(1) if sm else ""             # empty => llama.cpp default
    print(f"{name}\t{gguf}\t{dev}\t{sm}")
PY
}

# --- arg parsing --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)   DO_ALL=1; shift ;;
    --list)  DO_LIST=1; shift ;;
    --free)  DO_FREE=1; shift ;;
    -p|--prompt) PROMPT="$2"; shift 2 ;;
    -n|--ngen)   NGEN="$2"; shift 2 ;;
    -r|--reps)   REPS="$2"; shift 2 ;;
    -d|--depths) DEPTHS_STR="$2"; shift 2 ;;
    -o|--out)    OUT_DIR="$2"; shift 2 ;;
    -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
    -*)          die "unknown option: $1" ;;
    *)           REQUESTED+=("$1"); shift ;;
  esac
done

command -v python3 >/dev/null || die "python3 required (for reading $CONFIG)"
[[ -f "$CONFIG" ]] || die "config not found: $CONFIG"
[[ -x "$BIN" ]] || die "llama-bench not found/executable: $BIN"

# Load registry into parallel arrays.
declare -A GGUF DEV SM
ORDER=()
while IFS=$'\t' read -r name gguf dev sm; do
  [[ -z "$name" ]] && continue
  GGUF["$name"]="$gguf"; DEV["$name"]="$dev"; SM["$name"]="$sm"
  ORDER+=("$name")
done < <(registry)
[[ ${#ORDER[@]} -gt 0 ]] || die "no models parsed from $CONFIG"

if [[ $DO_LIST -eq 1 ]]; then
  printf "%-22s %-8s %-7s %s\n" "MODEL" "GPU(s)" "SPLIT" "GGUF"
  for n in "${ORDER[@]}"; do
    miss=""; [[ -f "${GGUF[$n]}" ]] || miss="  (MISSING)"
    printf "%-22s %-8s %-7s %s%s\n" "$n" "${DEV[$n]:-all}" "${SM[$n]:--}" "${GGUF[$n]}" "$miss"
  done
  exit 0
fi

# Which models to bench.
TARGETS=()
if [[ $DO_ALL -eq 1 ]]; then
  TARGETS=("${ORDER[@]}")
elif [[ ${#REQUESTED[@]} -gt 0 ]]; then
  for n in "${REQUESTED[@]}"; do
    [[ -n "${GGUF[$n]:-}" ]] || die "unknown model '$n' (see --list)"
    TARGETS+=("$n")
  done
else
  for n in "${DAILY[@]}"; do [[ -n "${GGUF[$n]:-}" ]] && TARGETS+=("$n"); done
fi
[[ ${#TARGETS[@]} -gt 0 ]] || die "no models to bench"

read -r -a DEPTHS <<< "$DEPTHS_STR"
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export PATH="/usr/local/cuda/bin:$PATH"

OUT_DIR="${OUT_DIR:-$MODELS_ROOT/bench-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT_DIR"
RESULTS="$OUT_DIR/results.md"

if [[ $DO_FREE -eq 1 ]]; then
  log "Unloading all llama-swap models (freeing GPUs before bench)"
  curl -s -X POST "$LLAMASWAP_URL/api/models/unload" -o /dev/null \
    -H 'content-type: application/json' -d '{}' || echo "  (unload request failed — continuing)"
  sleep 3
fi

{
  echo "# llama.cpp model benchmark"
  echo ""
  echo "- Date: $(date)"
  echo "- Host: $(hostname)"
  echo "- Driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  echo "- llama.cpp: $(git -C "$(dirname "$(dirname "$(dirname "$BIN")")")" describe --tags --always 2>/dev/null || echo unknown)"
  echo "- Params: -p $PROMPT -n $NGEN -r $REPS ; depths: ${DEPTHS[*]}"
  echo "- Models: ${TARGETS[*]}"
} > "$RESULTS"

BASE_PARAMS=(-p "$PROMPT" -n "$NGEN" -r "$REPS" -ngl 99)

for n in "${TARGETS[@]}"; do
  gguf="${GGUF[$n]}"; dev="${DEV[$n]}"; sm="${SM[$n]}"
  if [[ ! -f "$gguf" ]]; then
    log "SKIP $n — gguf missing: $gguf"; echo -e "\n### $n — SKIPPED (missing $gguf)\n" >> "$RESULTS"; continue
  fi
  log "$n   GPU(s)=${dev:-all}  split=${sm:-default}"
  echo -e "\n### $n  (GPU=${dev:-all}, split=${sm:-default})\n" >> "$RESULTS"
  extra=(); [[ -n "$sm" ]] && extra+=(-sm "$sm")
  for d in "${DEPTHS[@]}"; do
    echo "  depth=$d ..."
    if ! CUDA_VISIBLE_DEVICES="$dev" "$BIN" -m "$gguf" "${BASE_PARAMS[@]}" -d "$d" "${extra[@]}" -o md \
        | tee -a "$RESULTS"; then
      echo "  (config failed — likely OOM — continuing)" | tee -a "$RESULTS"
    fi
  done
done

log "Done. Results -> $RESULTS"

#!/usr/bin/env bash
# Back-compat shim -> power-cap-sweep.py (see its --help).
#
# The sweep moved to Python so it can set caps THROUGH gpu-fan-control's config:
# the daemon re-applies its configured cap every 30 s, so the old raw
# `nvidia-smi -pl` silently reverted mid-run. No sudo needed any more. It also
# runs several GPUs in lockstep (--gpus 1 2), records fan RPM/duty + throttle
# reasons, has a --soak steady-state phase, and writes CSVs to docs/data/thermal/.
#
# The old env knobs still work: GPU CAPS MODEL BIN NGL PP NG COOL_TO COOL_MAX
# THROTTLE_C. Extra args pass through, e.g.:
#   GPU=2 scripts/power-cap-sweep.sh --soak 600 --label blower-idx2
set -euo pipefail
args=()
[[ -n "${GPU:-}" ]] && args+=(--gpus ${GPU//,/ })
[[ -n "${CAPS:-}" ]] && args+=(--caps $CAPS)
[[ -n "${MODEL:-}" ]] && args+=(--model "$MODEL")
[[ -n "${BIN:-}" ]] && args+=(--bin "$BIN")
[[ -n "${NGL:-}" ]] && args+=(--ngl "$NGL")
[[ -n "${PP:-}" ]] && args+=(--pp "$PP")
[[ -n "${NG:-}" ]] && args+=(--ng "$NG")
[[ -n "${COOL_TO:-}" ]] && args+=(--cool-to "$COOL_TO")
[[ -n "${COOL_MAX:-}" ]] && args+=(--cool-max "$COOL_MAX")
[[ -n "${THROTTLE_C:-}" ]] && args+=(--throttle-c "$THROTTLE_C")
exec python3 "$(dirname "$(readlink -f "$0")")/power-cap-sweep.py" "${args[@]}" "$@"

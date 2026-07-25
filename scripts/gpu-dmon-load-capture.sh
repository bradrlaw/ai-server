#!/usr/bin/env bash
# Capture GPU core (gtemp) + HBM (mtemp) temperatures at idle and under a
# sustained compute load, then summarise the idle->load delta per GPU. Useful
# for A/B-ing cooling changes (e.g. a new case fan) where the meaningful signal
# is HBM temp under load, not at idle.
#
# It drives *real* decode load through the already-running llama-swap router
# (no model unload / no serving disruption): it fires long generations at one
# or more resident models concurrently, each pinned to its own GPU by the
# router, so the cards those models live on get pegged.
#
#   scripts/gpu-dmon-load-capture.sh [seconds] [outdir] [model ...]
#
# Defaults: 75s load window, outdir docs/data/thermal/, models "coding chat"
# (idx1 + idx2 V100s). Add/replace models to target other cards, e.g.
#   scripts/gpu-dmon-load-capture.sh 90 /tmp/cap fast        # P100 idx0
#   scripts/gpu-dmon-load-capture.sh 90 /tmp/cap coding chat # both V100s
set -u
cd "$(dirname "$0")/.."
SECONDS_LOAD="${1:-75}"
OUTDIR="${2:-docs/data/thermal}"
shift 2>/dev/null || true; shift 2>/dev/null || true
MODELS=("$@"); [ ${#MODELS[@]} -eq 0 ] && MODELS=(coding chat)
SWAP="${SWAP:-http://127.0.0.1:9090}"
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUTDIR"
IDLE="$OUTDIR/dmon-$STAMP.idle"
LOAD="$OUTDIR/dmon-$STAMP.load"

echo ">> models under load: ${MODELS[*]}   window: ${SECONDS_LOAD}s"
echo ">> idle baseline (8s)"
nvidia-smi dmon -s put -c 8 -d 1 > "$IDLE"

PROMPT='Write an extremely long, detailed technical essay (at least 5000 words) on the complete history of computing hardware from the abacus to modern GPUs. Include many sections and elaborate on each in depth.'
req() { # $1 model
  curl -s -m "$((SECONDS_LOAD+60))" "$SWAP/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":6000,\"temperature\":0.7}" \
    >/dev/null 2>&1
}
loop_model() { # keep a model busy until the deadline
  local m="$1" end=$(( $(date +%s) + SECONDS_LOAD ))
  while [ "$(date +%s)" -lt "$end" ]; do req "$m"; done
}

echo ">> starting load..."
pids=()
for m in "${MODELS[@]}"; do loop_model "$m" & pids+=($!); done
sleep 3  # let the first tokens start flowing before we start the load capture
nvidia-smi dmon -s put -c "$SECONDS_LOAD" -d 1 > "$LOAD"
echo ">> load window done; waiting for in-flight requests to finish"
for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
wait 2>/dev/null

python3 - "$IDLE" "$LOAD" <<'PY'
import sys, statistics as st
def parse(path):
    g={0:[],1:[],2:[]}; m={0:[],1:[],2:[]}; p={0:[],1:[],2:[]}; sm={0:[],1:[],2:[]}
    for ln in open(path):
        if ln.startswith('#'): continue
        c=ln.split()
        if len(c)<5: continue
        try: i=int(c[0])
        except: continue
        if c[2]!='-': g[i].append(int(c[2]))
        if c[3]!='-': m[i].append(int(c[3]))
        if c[1]!='-': p[i].append(int(c[1]))
        if c[4]!='-': sm[i].append(int(c[4]))
    return g,m,p,sm
ig,im,ip,ism = parse(sys.argv[1])
lg,lm,lp,lsm = parse(sys.argv[2])
name={0:'P100 idx0',1:'V100 idx1',2:'V100 idx2'}
def mean(x): return st.mean(x) if x else float('nan')
def mx(x): return max(x) if x else float('nan')
# drop first 5 load samples (thermal ramp) for the steady comparison
def steady(x): return x[5:] if len(x)>8 else x
print(f"\n{'GPU':11} {'idle gt':>7} {'load gt':>10} {'idle mt':>8} {'load mt(max)':>13} {'load sm%':>9} {'load W':>7}")
for i in (0,1,2):
    if not lg[i]: continue
    lgt=steady(lg[i]); lmt=steady(lm[i]) if lm[i] else []
    gi=mean(ig[i]); gl=mean(lgt)
    mi=mean(im[i]) if im[i] else float('nan'); ml=mean(lmt) if lmt else float('nan'); mmx=mx(lmt) if lmt else float('nan')
    dm = (ml-mi) if lmt and im[i] else float('nan')
    print(f"{name[i]:11} {gi:7.1f} {gl:6.1f}(+{gl-gi:.1f}) {mi:8.1f} "
          f"{ml:6.1f}/{mmx:.0f}(+{dm:.1f}) {mean(lsm[i]):9.0f} {mean(lp[i]):7.0f}")
print("\n(gt=core temp, mt=HBM/memory temp; load values skip first 5 ramp samples.")
print(" HBM throttles ~85C on the V100s — that's the number the side fan protects.)")
PY
echo ">> logs: $IDLE  $LOAD"

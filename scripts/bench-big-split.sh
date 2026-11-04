#!/usr/bin/env bash
# A/B the `big` weights (Qwen3.8-27B UD-Q8_K_XL) on both V100s: `-sm layer` (production)
# vs `-sm tensor` (llama.cpp's experimental tensor parallelism), with PCIe sampling.
# Run once on the current x8 links (label x8) and again after the V100s move to x16
# (label x16) to see whether x16 makes tensor split worth it. No sudo needed.
#
# Usage: scripts/bench-big-split.sh <label> [--unload] [--restore]
#        scripts/bench-big-split.sh compare <labelA> <labelB>
#   --unload   POST llama-swap /api/models/unload first (needed if flash/big/coding/chat are loaded;
#              this fails any session using them). Without it the script aborts unless
#              only `small` (idx0, unaffected) is running.
#   --restore  run `llama-swap-mode.py set daily` afterwards (reloads the trio).
# Env:   LLAMA_BENCH_BIN (default: production build; use a separate build for A/B),
#        REPS (default 3), PROMPTS (default 512,4096,16384), DEPTHS (default 0,8192),
#        NGEN (default 128).
#
# Notes: llama-bench has no MTP, so this isolates the split mode itself (prefill = PCIe-
# heavy, decode = per-token sync latency). Production `big` also uses draft-mtp; whether
# MTP works with -sm tensor is a separate test (llama-server). Results: docs/data/big-split/.
# `-sm row` was ~4x slower prefill on this box in July (benchmarking.md); `tensor` is a
# different implementation and has not been tried here before.
set -u
export CUDA_DEVICE_ORDER=PCI_BUS_ID
ROOT=/srv/ai
OUT=${BIG_SPLIT_OUT:-$ROOT/docs/data/big-split}
BIN=${LLAMA_BENCH_BIN:-$ROOT/src/llama.cpp/build/bin/llama-bench}
MODEL=$ROOT/models/qwen3.8-27b/Qwen3.8-27B-UD-Q8_K_XL.gguf
V100_IDX=${V100_IDX:-1,2}   # GPU indices of the two V100s (0,1 while the Titan is out)
REPS=${REPS:-3}; PROMPTS=${PROMPTS:-512,4096,16384}; DEPTHS=${DEPTHS:-0,8192}; NGEN=${NGEN:-128}
SWAP=http://127.0.0.1:9090

summarize() { # <csv> ...
python3 - "$@" <<'PY'
import csv, sys
for path in sys.argv[1:]:
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        kind = "pp" if int(r["n_prompt"]) > 0 else "tg"
        n = r["n_prompt"] if kind == "pp" else r["n_gen"]
        print(f'{path.split("/")[-1]:<26} {kind}{n:<6} depth {r["n_depth"]:<6} {float(r["avg_ts"]):>9.1f} t/s  (+-{float(r["stddev_ts"]):.1f})')
PY
}

if [ "${1:-}" = compare ]; then
python3 - "$OUT" "$2" "$3" <<'PY'
import csv, sys, os
out, a, b = sys.argv[1:4]
def load(label):
    d = {}
    for mode in ("layer", "tensor"):
        p = f"{out}/{label}-{mode}.csv"
        if not os.path.exists(p): continue
        for r in csv.DictReader(open(p)):
            kind = "pp" if int(r["n_prompt"]) > 0 else "tg"
            n = r["n_prompt"] if kind == "pp" else r["n_gen"]
            d[(kind + n, int(r["n_depth"]), mode)] = float(r["avg_ts"])
    return d
A, B = load(a), load(b)
keys = sorted({k[:2] for k in A | B}, key=lambda k: (k[0][:2] != "pp", int(k[0][2:]), k[1]))
h = f'{"test":<10}{"depth":>6} | {a+" layer":>11}{a+" tensor":>12}{"t/l":>6} | {b+" layer":>11}{b+" tensor":>12}{"t/l":>6}'
print(h)
g = lambda D, t, d, m: D.get((t, d, m))
for t, d in keys:
    cells = []
    for D in (A, B):
        l, s = g(D, t, d, "layer"), g(D, t, d, "tensor")
        cells.append(f'{l if l else "-":>11}{s if s else "-":>12}{(f"{s/l:.2f}x" if l and s else "-"):>6}')
    print(f"{t:<10}{d:>6} | " + " | ".join(cells))
PY
exit 0
fi

label=${1:?usage: $0 <label> [--unload] [--restore] | compare <a> <b>}; shift
UNLOAD=0; RESTORE=0
for a in "$@"; do case $a in --unload) UNLOAD=1;; --restore) RESTORE=1;; *) echo "unknown arg $a"; exit 2;; esac; done
[ -x "$BIN" ] || { echo "no llama-bench at $BIN"; exit 1; }
[ -f "$MODEL" ] || { echo "model missing: $MODEL"; exit 1; }
mkdir -p "$OUT"

running=$(curl -s --max-time 5 $SWAP/running | python3 -c "import json,sys;print(' '.join(m['model'] for m in json.load(sys.stdin).get('running',[])))")
others=$(echo "$running" | tr ' ' '\n' | grep -vx -e small -e small-uncensored -e '' | tr '\n' ' ')
if [ -n "$others" ]; then
  [ $UNLOAD -eq 1 ] || { echo "Loaded on the V100s: $others"; echo "Stop sessions using them, then re-run with --unload"; exit 1; }
  echo "unloading: $others"; curl -s -X POST --max-time 120 $SWAP/api/models/unload >/dev/null
  for _ in $(seq 30); do [ "$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | grep -E "^($(echo $V100_IDX | tr , '|'))," | awk "{s+=\$1} END{print s}")" -lt 2000 ] && break; sleep 2; done
fi
used=$(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits | awk -F', ' -v ids=",$V100_IDX," 'index(ids, ","$1","){s+=$2} END{print s}')
[ "$used" -lt 2000 ] || { echo "V100s still hold ${used} MiB; something is using them"; exit 1; }

echo "== links, before load =="
nvidia-smi --query-gpu=index,name,pcie.link.gen.current,pcie.link.width.current --format=csv,noheader | sed -n 2,3p
nvidia-smi dmon -s t -d 1 >"$OUT/$label-pcie.txt" 2>&1 &
DMON=$!
nvidia-smi --query-gpu=index,pcie.link.gen.current,pcie.link.width.current --format=csv,noheader,nounits -l 1 >"$OUT/$label-link.txt" 2>&1 &
LINK=$!
trap 'kill $DMON $LINK 2>/dev/null' EXIT

for mode in layer tensor; do
  echo "== -sm $mode =="
  CUDA_VISIBLE_DEVICES=$V100_IDX "$BIN" -m "$MODEL" -sm $mode -ngl 99 -fa on -b 2048 -ub 2048 \
    -ctk f16 -ctv f16 -p "$PROMPTS" -n "$NGEN" -d "$DEPTHS" -r "$REPS" -o csv \
    >"$OUT/$label-$mode.csv" 2>"$OUT/$label-$mode.err"
  rc=$?
  if [ $rc -ne 0 ] || ! grep -q avg_ts "$OUT/$label-$mode.csv"; then
    echo "  -sm $mode FAILED (rc=$rc); see $OUT/$label-$mode.err"; tail -3 "$OUT/$label-$mode.err" | sed 's/^/    /'
    rm -f "$OUT/$label-$mode.csv"
    continue
  fi
  summarize "$OUT/$label-$mode.csv"
done
kill $DMON $LINK 2>/dev/null

echo "== PCIe rx peaks (MB/s, per V100) and link seen =="
awk -v ids=",$V100_IDX," 'index(ids, ","$1",") { if($2+0>m[$1]) m[$1]=$2+0 } END{for(i in m) print "idx" i, "rx peak", m[i]}' "$OUT/$label-pcie.txt"
awk -F', ' '{if($3>w[$1]) {w[$1]=$3; g[$1]=$2}} END{for(i in w) print "idx" i, "gen" g[i], "x" w[i], "(max seen)"}' "$OUT/$label-link.txt" | sort | grep -E "idx($(echo $V100_IDX | tr , '|'))\b"
echo "results in $OUT/$label-*.csv  (compare: $0 compare x8 x16)"
[ $RESTORE -eq 1 ] && { echo "restoring daily"; python3 $ROOT/scripts/llama-swap-mode.py set daily >/dev/null && echo done; }
exit 0

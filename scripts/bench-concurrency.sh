#!/usr/bin/env bash
# Concurrency / throughput-scaling benchmark for the AI server.
#
# Wraps Alex Ziskind's llm-scaling-bench (https://github.com/alexziskind1/llm-scaling-bench)
# and points it at an OpenAI-compatible endpoint — by default LiteLLM on :4000.
# It sweeps N concurrent requests and reports aggregate tokens/sec + success rate,
# reproducing the "does this server actually parallelise?" curve.
#
# NOTE ON RESULTS: a model with `--parallel 1` in config/llama-swap.yaml SERIALISES —
# aggregate tokens/sec stays flat as concurrency rises and the stack returns 429s once
# the single slot's queue overflows. To measure real engine concurrency, raise
# `--parallel N` on the model block and re-run.
#
# Agents can't sudo and python3-venv isn't installed, so this bootstraps pip into a
# plain venv via get-pip.py.
#
# Usage:
#   scripts/bench-concurrency.sh                      # coding, [1,2,4,8,16] via LiteLLM
#   BENCH_MODEL=chat scripts/bench-concurrency.sh
#   BENCH_USERS="1,2,4,8,16,32" BENCH_MODEL=fast scripts/bench-concurrency.sh
#   BENCH_API_URL=http://127.0.0.1:9090/v1/chat/completions scripts/bench-concurrency.sh  # hit llama-swap directly
set -euo pipefail

AI_ROOT="/srv/ai"
BENCH_DIR="${AI_ROOT}/benchmarks/llm-scaling-bench"
REPO="https://github.com/alexziskind1/llm-scaling-bench"

BENCH_MODEL="${BENCH_MODEL:-coding}"
BENCH_USERS="${BENCH_USERS:-1,2,4,8,16}"
BENCH_MAX_TOKENS="${BENCH_MAX_TOKENS:-512}"
export BENCH_MODEL BENCH_MAX_TOKENS
export BENCH_API_URL="${BENCH_API_URL:-http://127.0.0.1:4000/v1/chat/completions}"

# --- key: prefer explicit BENCH_API_KEY, else LiteLLM master key from docker/.env ---
if [[ -z "${BENCH_API_KEY:-}" ]]; then
  if [[ -f "${AI_ROOT}/docker/.env" ]]; then
    set -a; # shellcheck disable=SC1091
    source "${AI_ROOT}/docker/.env"; set +a
  fi
  export BENCH_API_KEY="${LITELLM_MASTER_KEY:-test}"
fi

# --- clone the harness on first run ---
if [[ ! -d "${BENCH_DIR}/.git" ]]; then
  echo "[bench] cloning ${REPO} ..."
  mkdir -p "${AI_ROOT}/benchmarks"
  git clone --depth 1 "${REPO}" "${BENCH_DIR}"
fi
cd "${BENCH_DIR}"

# --- venv + pip bootstrap (python3-venv/ensurepip may be missing) ---
if [[ ! -x .venv/bin/python ]]; then
  echo "[bench] creating venv ..."
  python3 -m venv --without-pip .venv
  curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  .venv/bin/python /tmp/get-pip.py -q
fi
if ! .venv/bin/python -c "import aiohttp, pandas, plotly" 2>/dev/null; then
  echo "[bench] installing requirements ..."
  .venv/bin/python -m pip install -q -r requirements.txt
fi

# --- env-driven benchmark variant (no hardcoded secrets) ---
if [[ ! -f benchmarks/bench_aiserver.py ]]; then
  cp benchmarks/bench_concurrent_scaling.py benchmarks/bench_aiserver.py
  python3 - <<'PY'
p = "benchmarks/bench_aiserver.py"
s = open(p).read()
old = '''# --- Configuration ---
API_URL = "http://localhost:12434/engines/llama.cpp/v1/chat/completions"
API_KEY = "test"
MODEL_NAME = "ai/qwen2.5"
PROMPT = "write me a 1000 word essay on AI"
MAX_TOKENS_PER_RESPONSE = 512'''
new = '''# --- Configuration (env-driven; defaults target LiteLLM on this server) ---
API_URL = os.environ.get("BENCH_API_URL", "http://127.0.0.1:4000/v1/chat/completions")
API_KEY = os.environ.get("BENCH_API_KEY", os.environ.get("LITELLM_MASTER_KEY", "test"))
MODEL_NAME = os.environ.get("BENCH_MODEL", "coding")
PROMPT = os.environ.get("BENCH_PROMPT", "write me a 1000 word essay on AI")
MAX_TOKENS_PER_RESPONSE = int(os.environ.get("BENCH_MAX_TOKENS", "512"))'''
assert old in s, "config block not found — upstream layout changed"
open(p, "w").write(s.replace(old, new))
print("[bench] wrote env-driven benchmarks/bench_aiserver.py")
PY
fi

# --- set the concurrency sweep from BENCH_USERS ---
python3 - "$BENCH_USERS" <<'PY'
import re, sys
users = "[" + ", ".join(u.strip() for u in sys.argv[1].split(",") if u.strip()) + "]"
p = "config/benchmark_config.py"
s = open(p).read()
s = re.sub(r"(?m)^CONCURRENT_USER_COUNTS\s*=.*$", f"CONCURRENT_USER_COUNTS = {users}", s)
open(p, "w").write(s)
print(f"[bench] CONCURRENT_USER_COUNTS = {users}")
PY

echo "[bench] model=${BENCH_MODEL} url=${BENCH_API_URL} users=${BENCH_USERS} max_tokens=${BENCH_MAX_TOKENS}"
echo "[bench] running (unbuffered) ..."
.venv/bin/python -u benchmarks/bench_aiserver.py

echo "[bench] latest CSV:"; ls -t results/*.csv 2>/dev/null | head -1
echo "[bench] plot: .venv/bin/python scripts/plot_results.py --latest  (HTML in results/)"

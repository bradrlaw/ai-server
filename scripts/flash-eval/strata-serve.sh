#!/bin/bash
# usage: strata-serve.sh <config.json>   (run only inside an eval window: stops nothing, needs idle V100s)
set -e
export CUDA_DEVICE_ORDER=PCI_BUS_ID
cd /srv/ai/src/strata
exec /srv/ai/venvs/strata/bin/python -m serve.server --engine strata --config "$1" --host 127.0.0.1 --port 8080

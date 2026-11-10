#!/usr/bin/env bash
# One ephemeral runner per loop: fetch a fresh registration token, register
# --ephemeral (GitHub removes it after one job), run, then start over.
set -u
: "${GH_REPO:?set GH_REPO (owner/name)}" "${GH_RUNNER_PAT:?set GH_RUNNER_PAT}"
LABELS="${RUNNER_LABELS:-binit-runner}"
cd /home/runner/actions-runner
while true; do
  tok=$(curl -fsS -X POST \
    -H "Authorization: Bearer ${GH_RUNNER_PAT}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${GH_REPO}/actions/runners/registration-token" | jq -r .token)
  if [ -z "$tok" ] || [ "$tok" = null ]; then
    echo "registration-token request failed; retrying in 60 s" >&2
    sleep 60; continue
  fi
  rm -rf _work .runner .credentials .credentials_rsaparams
  ./config.sh --unattended --ephemeral --replace \
    --url "https://github.com/${GH_REPO}" --token "$tok" \
    --name "aipcub-$(hostname)" --labels "$LABELS" --no-default-labels \
    || { echo "config.sh failed; retrying in 60 s" >&2; sleep 60; continue; }
  unset tok
  ./run.sh
  sleep 2
done

# ADR-0039: Self-hosted GitHub Actions runner for bradrlaw/binit

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The private `bradrlaw/binit` repo (web app deployed to Azure) used up its GitHub-hosted
Actions minutes. A runner can be any machine with outbound HTTPS to GitHub, so this
server can take the heavy, stateless jobs. The Copilot agent working on `binit` proposed
a hybrid split; this ADR adopts it and adds isolation suited to a box that also holds
model weights, the unlocked LUKS volume and `docker/.env`.

## Decision
- **Routing (made in the `binit` repo, not here):** E2E smoke (nightly, 06:17 UTC) and
  a11y (every PR) use `runs-on: binit-runner`. CI and Deploy stay GitHub-hosted: Deploy
  holds the Azure credentials, and PR code must not run on the machine that can deploy.
- **Runner = Compose service `gh-runner`** (`docker/gh-runner/`, profile `runner`, opt-in):
  Playwright noble base image (browser OS deps, no host `apt`), pinned runner release with
  checksum, uid 10002, `cap_drop: ALL`, `no-new-privileges`, 4 CPUs / 8 GiB / 1024 pids,
  `/tmp` tmpfs, **no published ports, no host mounts, no Docker socket**.
- **Ephemeral:** `entrypoint.sh` fetches a fresh registration token, registers
  `--ephemeral`, runs one job, and loops. Every job starts from a clean container
  filesystem.
- **Auth:** `GH_RUNNER_PAT` in `docker/.env` (gitignored): fine-grained PAT scoped to the
  single repo `bradrlaw/binit`, permission Administration: read/write (GitHub's
  requirement for runner registration). Only this service receives it.
- **Label:** `binit-runner` only. The iMac should use its own label, so the nightly does
  not land on a sleeping Mac.
- **Network:** egress only. ADR-0028 limits container→host ports and the runner needs
  none, so `setup-firewall.sh` / `verify-firewall.sh` are unchanged.
- **Azure:** use OIDC federated credentials in the (hosted) Deploy job; no long-lived
  Azure secret lives here.

## Consequences
- Positive: nightly E2E and per-PR a11y stop consuming hosted minutes; clean per-job state;
  blast radius limited to a resource-capped container.
- Negative / trade-offs: the PAT can administer runners on `binit` (cannot read code with
  this permission alone); jobs share the host kernel; Chromium and Strata (`flash` pins
  about 72 GiB RAM) compete for RAM/CPU, so the 8 GiB cap is a hard limit; GitHub has
  announced a per-minute platform charge for self-hosted runners, so check current billing.
- Follow-ups / things to watch: private repos only (never a public repo or fork PRs);
  jobs that build images need a rootless builder, not the host socket; re-pin
  `RUNNER_VERSION`/`RUNNER_SHA256` when GitHub's minimum runner version moves.

## Alternatives considered
- **Bare-host install (agent's proposal):** needs host `sudo` for Playwright deps and runs
  PR code as `brad` next to secrets and the LUKS volume. Rejected.
- **Runner on the iMac only:** asleep at 1:17 am ET. Kept as an optional second runner.
- **Move CI/Deploy too:** fast hosted isolation and Azure secrets favour hosted. Rejected.
- **Persistent (non-ephemeral) runner:** state leaks between jobs. Rejected.

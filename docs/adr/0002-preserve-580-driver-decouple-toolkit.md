# ADR-0002: Preserve nvidia-580-server driver; decouple CUDA toolkit

- **Status:** Accepted
- **Date:** 2026-06-30
- **Deciders:** @bradrlaw (+ Copilot CLI)

## Context
The `nvidia-driver-580-server` (580.159.03) driver was already installed and
working: `nvidia-smi` detects all three GPUs correctly. A prior CUDA install had
gone wrong, so there was temptation to "start clean" including the driver. But the
NVIDIA **driver and CUDA toolkit are independently versioned**: a 580 driver
(CUDA-13-capable) is backward compatible with CUDA 12.x toolkits.

## Decision
**Keep the 580 driver untouched.** Only add/remove the **CUDA toolkit** layer.
When installing CUDA, use the **toolkit-only** metapackage (`cuda-toolkit-12-9`),
never the `cuda` / `cuda-12-9` / `cuda-drivers` metapackages that would pull a
(different) driver.

All CUDA install/uninstall scripts must:
1. Record the driver package set before and after and diff it.
2. Simulate (`apt-get -s`) and **abort** if any `nvidia*/-580/kernel/dkms/firmware`
   package would be added, removed, or changed.
3. Verify `nvidia-smi` still works afterward.

## Consequences
- Positive: a known-good, stable driver is never put at risk during CUDA churn.
- Positive: CUDA versions become freely swappable without touching the kernel.
- Watch: if a future framework needs a newer driver, that becomes its own ADR.

## Alternatives considered
- **Reinstall driver + CUDA together via the `cuda` metapackage** - rejected;
  risks replacing a working driver and conflates two independent concerns.

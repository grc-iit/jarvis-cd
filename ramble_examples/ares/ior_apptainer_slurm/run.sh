#!/usr/bin/env bash
# Launch the IOR-apptainer-SLURM experiment on ares.
#
# Preconditions:
#   1. Ramble is on PATH (source ramble/share/ramble/setup-env.sh from
#      the submodule, or pip-install ramble).
#   2. The Jarvis IOR container has been built once, producing
#      ~/.jarvis/shared/ior_ares_container_test/ior_ares_container_test.sif.
#      If the SIF is missing, Ramble will fall back to `apptainer pull`
#      using the container_uri in ramble.yaml.
#   3. `sbatch`, `squeue`, `sacct`, `apptainer` are available on the login
#      node where this script runs.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${WORKSPACE:-$HOME/ramble-workspaces/ior_apptainer_slurm_ares}"
SIF="$HOME/.jarvis/shared/ior_ares_container_test/ior_ares_container_test.sif"

if [[ ! -f "$SIF" ]]; then
  echo "[warn] Jarvis SIF not found at $SIF"
  echo "[warn] Build it first with:  jarvis ppl load ior_container_test && jarvis ppl run"
  echo "[warn] Continuing -- ramble will try to pull container_uri instead."
fi

echo "[info] creating ramble workspace at $WORKSPACE"
ramble workspace create -d "$WORKSPACE" -c "$HERE/ramble.yaml"

echo "[info] setting up experiments (renders sbatch scripts, pulls SIF if missing)"
ramble -w "$WORKSPACE" workspace setup

echo "[info] submitting experiments via sbatch"
ramble -w "$WORKSPACE" on

echo "[info] waiting for jobs to complete"
ramble -w "$WORKSPACE" workspace analyze --wait

echo "[info] results:"
ramble -w "$WORKSPACE" results

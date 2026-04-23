#!/bin/bash
# run_demo_multinode.sh — drive the idealized MCS demo under Dask-MPI
# across head + worker SSH hosts.
#
# Usage: run_demo_multinode.sh <data_root> <host1[,host2,...]> [np]
#   data_root : shared output directory (must be visible from every host)
#   hosts     : comma-separated MPI host list (head is first)
#   np        : total MPI ranks (default 4 → 1 sched + 1 client + 2 workers)
#
# Layout:
#   1. Use upstream tests/run_demo_tests.py to download the idealized data
#      and template its config. Serial seed pass; clean under $DATA_ROOT.
#   2. Flip run_parallel 0 → 2 in the templated config.
#   3. Wipe the seed's stats/ output.
#   4. mpirun -host <hosts> -np $np python /opt/run_mcs_tbpf_mpi.py <config>
#   5. Assert stats/ is re-populated by the MPI run.

set -euo pipefail

DATA_ROOT="${1:-/output/data}"
HOSTS="${2:-}"
NP="${3:-4}"
DEMO="demo_mcs_tbpf_idealized"

if [ -z "$HOSTS" ]; then
    echo "usage: $0 <data_root> <host1,host2,...> [np]" >&2
    exit 2
fi

mkdir -p "$DATA_ROOT"
cd /opt/PyFLEXTRKR

# --- 1. Seed: download + template config via upstream harness (serial). ---
echo "=== seed (serial) to download + template config ==="
python tests/run_demo_tests.py --demos "$DEMO" --data-root "$DATA_ROOT" -n 1

DEMO_DIR="$DATA_ROOT/mcs_tbpf/idealized/test4"
CONFIG=$(find "$DEMO_DIR" -maxdepth 2 -name 'config_mcs_idealized.yml' | head -1)
if [ -z "$CONFIG" ]; then
    echo "FAIL: could not locate templated config under $DEMO_DIR" >&2
    exit 1
fi
echo "config: $CONFIG"

# --- 2. Flip to Dask-MPI mode. ---
sed -i 's/^run_parallel:.*/run_parallel: 2/' "$CONFIG"
grep -E '^run_parallel|^nprocesses|^timeout' "$CONFIG" || true

# --- 3. Wipe serial-seed outputs so the MPI pass re-writes them. ---
rm -rf "$DEMO_DIR/stats" "$DEMO_DIR/tracking" "$DEMO_DIR/mcstracking" \
       "$DEMO_DIR/mcstracking_tb" "$DEMO_DIR/ccstracking"

# --- 4. mpirun across the host list. ---
export HDF5_USE_FILE_LOCKING=FALSE
echo "=== mpirun -np $NP -host $HOSTS python run_mcs_tbpf_mpi.py ==="
mpirun -np "$NP" -host "$HOSTS" \
    --allow-run-as-root \
    -x HDF5_USE_FILE_LOCKING=FALSE \
    -x PATH -x LD_LIBRARY_PATH \
    python /opt/run_mcs_tbpf_mpi.py "$CONFIG"

# --- 5. Assert the MPI pass produced stats again. ---
STATS_DIR="$DEMO_DIR/stats"
if [ -d "$STATS_DIR" ] && [ "$(ls -A "$STATS_DIR" 2>/dev/null)" ]; then
    echo "=== SUCCESS: multinode demo produced stats in $STATS_DIR ==="
    ls -la "$STATS_DIR"
    exit 0
fi
echo "FAIL: no stats files produced by MPI pass under $STATS_DIR" >&2
exit 1

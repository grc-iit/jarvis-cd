#!/bin/bash
# Wrapper around upstream PyFLEXTRKR demo runner.
# Usage: run_demo.sh <demo_name> <data_root> [<extra flags>...]
#
# Default demo is `demo_mcs_tbpf_idealized` (smallest, ~1-2 MB download).
# Runs serially (-n 1) to avoid the known HDF5 concurrent-access bug in the
# Dask local-cluster mode.
set -euo pipefail
DEMO="${1:-demo_mcs_tbpf_idealized}"
DATA_ROOT="${2:-/output/pyflextrkr-data}"
shift 2 || true

mkdir -p "$DATA_ROOT"
cd /opt/PyFLEXTRKR

python tests/run_demo_tests.py \
    --demos "$DEMO" \
    --data-root "$DATA_ROOT" \
    -n 1 \
    "$@"

# Minimal success check: stats directory non-empty.
STATS_DIR=$(find "$DATA_ROOT" -type d -name stats | head -1)
if [ -n "$STATS_DIR" ] && [ "$(ls -A "$STATS_DIR" 2>/dev/null)" ]; then
    echo "=== SUCCESS: $DEMO produced stats in $STATS_DIR ==="
    exit 0
fi
echo "FAIL: no stats files produced under $DATA_ROOT"
exit 1

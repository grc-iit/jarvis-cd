#!/bin/bash
# DDMD per-stage I/O task. Replaces the /bin/echo placeholders in the
# DDMD template. Every call reads/writes files under the CTE FUSE
# mountpoint so the wrp_cte_libfuse adapter is actually exercised.
#
# Usage: ddmd_io_task.sh <stage>
#   stage = md | ml | ms | agent
set -euo pipefail
STAGE="${1:?stage required (md|ml|ms|agent)}"

OUT_ROOT="${DDMD_CTE_OUT:-/mnt/wrp_cte/ddmd}"
mkdir -p "$OUT_ROOT"

# Unique ID per task invocation. radical.pilot sets RP_TASK_ID; fall
# back to pid + nanoseconds for direct invocations.
ID="${RP_TASK_ID:-$$_$(date +%s%N)}"
OUT="$OUT_ROOT/${STAGE}_${ID}.bin"

case "$STAGE" in
    md)
        # Fake MD frame output: 4 MB of random data per task. This is
        # what a real OpenMM simulation would stream as an H5 trajectory.
        head -c 4194304 /dev/urandom > "$OUT"
        ;;
    ml)
        # Fake ML stage: read all MD outputs, count bytes, write a tiny
        # "model" file. Read-heavy phase — exercises CTE GetBlob.
        total=0
        for f in "$OUT_ROOT"/md_*.bin; do
            [ -f "$f" ] || continue
            total=$(( total + $(stat -c '%s' "$f") ))
        done
        printf 'model_trained_on_%d_bytes\n' "$total" > "$OUT"
        ;;
    ms)
        # Model selection: enumerate ML outputs, write a selection record.
        {
            echo "# DDMD model selection"
            find "$OUT_ROOT" -maxdepth 1 -name 'ml_*.bin' -printf '%f %s\n'
        } > "$OUT"
        ;;
    agent)
        # Agent summary: final pass over everything produced this run.
        find "$OUT_ROOT" -maxdepth 1 -type f -printf '%f %s\n' \
            | sort > "$OUT"
        ;;
    *)
        echo "unknown stage: $STAGE" >&2
        exit 2
        ;;
esac

echo "[ddmd_io_task $STAGE $ID] wrote $OUT ($(stat -c '%s' "$OUT") bytes)"

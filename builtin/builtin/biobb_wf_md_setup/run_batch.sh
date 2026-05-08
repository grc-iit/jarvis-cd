#!/bin/bash
# Run biobb_wf_md_setup over a PDB input (single file or directory of
# .pdb), exercising the CTE FUSE adapter for durable outputs.
#
# Usage: run_batch.sh [<pdb_input> [<out_dir> [<hosts>]]]
#   pdb_input — single .pdb file or directory of .pdb files.
#               Default: /opt/biobb-bench (the bundled 1AKI lysozyme).
#   out_dir   — where to stage final outputs; intermediates live on /tmp.
#               Default: /mnt/wrp_cte/biobb_out (the CTE FUSE mount).
#   hosts     — comma-separated list for multi-node distribution (MPI-less
#               fan-out; empty = single-node).
set -euo pipefail

PDB_INPUT="${1:-/opt/biobb-bench}"
OUT_DIR="${2:-/mnt/wrp_cte/biobb_out}"
HOSTS="${3:-}"

# jarvis's docker-exec env does not inherit the image's ENV PATH, and
# jarvis's pipeline-merge drops ENV from non-first packages anyway, so
# prepend the biobb conda env explicitly. run_md_setup.py uses a
# /opt/biobb-env/bin/python shebang, but shell rules forked off it
# (e.g. gmx) still need the env on PATH.
export PATH="/opt/biobb-env/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"

# GROMACS and the biobb wrappers write many intermediate files and use
# atomic-write-then-rename patterns. wrp_cte_fuse returns ENOSYS on
# rename(2) (same constraint documented in metagem_cte / montage_cte),
# so run the actual pipeline in /tmp scratch (where rename works) and
# stage the finished per-PDB trees into the FUSE mount via plain cp —
# which is what exercises the CTE adapter.
SCRATCH="${BIOBB_SCRATCH_DIR:-/tmp/biobb-scratch}"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"

# Resolve input PDBs.
if [ -f "$PDB_INPUT" ]; then
    PDBS=("$PDB_INPUT")
elif [ -d "$PDB_INPUT" ]; then
    mapfile -t PDBS < <(ls "$PDB_INPUT"/*.pdb 2>/dev/null)
else
    echo "ERROR: pdb_input '$PDB_INPUT' is neither a file nor a directory" >&2
    exit 2
fi
[ ${#PDBS[@]} -gt 0 ] || { echo "ERROR: no .pdb files found under $PDB_INPUT" >&2; exit 2; }

PASS=0; FAIL=0
run_one() {
    local pdb=$1
    local tag; tag=$(basename "$pdb" .pdb)
    if [ -n "${REMOTE_HOST:-}" ]; then
        ssh -o StrictHostKeyChecking=no "$REMOTE_HOST" \
            "/opt/run_md_setup.py '$pdb' '$SCRATCH/$tag'"
    else
        /opt/run_md_setup.py "$pdb" "$SCRATCH/$tag"
    fi
}

if [ -z "$HOSTS" ]; then
    for p in "${PDBS[@]}"; do
        if run_one "$p"; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
    done
else
    IFS=',' read -r -a HARR <<< "$HOSTS"
    i=0
    for p in "${PDBS[@]}"; do
        h=${HARR[$(( i % ${#HARR[@]} ))]}
        REMOTE_HOST=$h run_one "$p" &
        i=$((i+1))
    done
    wait
    # Tally by presence of solvate output in scratch.
    PASS=0; FAIL=0
    for p in "${PDBS[@]}"; do
        tag=$(basename "$p" .pdb)
        if ls "$SCRATCH/$tag/"*_solvate.gro >/dev/null 2>&1; then
            PASS=$((PASS+1))
        else
            FAIL=$((FAIL+1))
        fi
    done
fi

# Stage the completed per-PDB trees into the FUSE mount. cp is open+
# write+close (no rename), which wrp_cte_fuse supports. This is what
# the wrp_cte_libfuse adapter sees — the .gro / .zip / .pdb outputs of
# each MD setup stage flow through CTE here.
echo "--- Staging results to $OUT_DIR ---"
mkdir -p "$OUT_DIR"
cp -r "$SCRATCH"/. "$OUT_DIR/"

CTE_COUNT=$(find "$OUT_DIR" -type f 2>/dev/null | wc -l)
CTE_BYTES=$(find "$OUT_DIR" -type f -printf '%s\n' 2>/dev/null | awk 'BEGIN{s=0}{s+=$1}END{print s}')
echo "=== BATCH DONE: pass=$PASS fail=$FAIL of ${#PDBS[@]} ==="
echo "=== CTE FUSE traffic: $CTE_COUNT files, $CTE_BYTES bytes under $OUT_DIR ==="
[ $PASS -ge 1 ] && exit 0 || exit 1

#!/bin/bash
# Run biobb_wf_md_setup over a PDB input (single file or directory of
# .pdb). All 5 pipeline stages (PDB fetch → fix-side-chains → pdb2gmx
# topology → editconf box → solvate) write their intermediates directly
# into $OUT_DIR/$tag/; each stage reads the previous stage's output back
# from there. The storage layer under $OUT_DIR sees the full producer-
# consumer chain, not just a final bulk cp.
#
# (Earlier revisions detoured intermediates through /tmp/biobb-scratch
# because wrp_cte_fuse returned ENOSYS on rename(2); GROMACS/biobb's
# atomic-write-then-rename then broke. Backends that implement rename(2)
# correctly — NFS, dumbwarp, ext4 — don't need that detour and shouldn't
# pay its cost in lost producer-consumer traffic.)
#
# Usage: run_batch.sh [<pdb_input> [<out_dir> [<hosts>]]]
#   pdb_input — single .pdb file or directory of .pdb files.
#               Default: /opt/biobb-bench (the bundled 1AKI lysozyme).
#   out_dir   — where every stage writes; the next stage reads from here.
#               Default: ${HOME}/biobb_out
#   hosts     — comma-separated list for multi-node distribution
#               (MPI-less fan-out; empty = single-node).
set -euo pipefail

PDB_INPUT="${1:-/opt/biobb-bench}"
OUT_DIR="${2:-${HOME}/biobb_out}"
HOSTS="${3:-}"

# jarvis's docker-exec env does not inherit the image's ENV PATH, and
# jarvis's pipeline-merge drops ENV from non-first packages anyway, so
# prepend the biobb conda env explicitly. run_md_setup.py uses a
# /opt/biobb-env/bin/python shebang, but shell rules forked off it
# (e.g. gmx) still need the env on PATH.
export PATH="/opt/biobb-env/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"

mkdir -p "$OUT_DIR"

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
            "/opt/run_md_setup.py '$pdb' '$OUT_DIR/$tag'"
    else
        /opt/run_md_setup.py "$pdb" "$OUT_DIR/$tag"
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
    # Tally by presence of solvate output.
    PASS=0; FAIL=0
    for p in "${PDBS[@]}"; do
        tag=$(basename "$p" .pdb)
        if ls "$OUT_DIR/$tag/"*_solvate.gro >/dev/null 2>&1; then
            PASS=$((PASS+1))
        else
            FAIL=$((FAIL+1))
        fi
    done
fi

OUT_COUNT=$(find "$OUT_DIR" -type f 2>/dev/null | wc -l)
OUT_BYTES=$(find "$OUT_DIR" -type f -printf '%s\n' 2>/dev/null | awk 'BEGIN{s=0}{s+=$1}END{print s}')
echo "=== BATCH DONE: pass=$PASS fail=$FAIL of ${#PDBS[@]} ==="
echo "=== Pipeline I/O traffic: $OUT_COUNT files, $OUT_BYTES bytes under $OUT_DIR ==="
[ $PASS -ge 1 ] && exit 0 || exit 1

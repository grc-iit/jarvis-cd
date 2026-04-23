#!/bin/bash
set -uo pipefail
PDB_DIR="${1:?pdb_dir required}"
OUT_DIR="${2:?out_dir required}"
HOSTS="${3:-}"
mkdir -p "$OUT_DIR"
mapfile -t PDBS < <(ls "$PDB_DIR"/*.pdb)
PASS=0; FAIL=0
run_one() {
    local pdb=$1; local tag=$(basename "$pdb" .pdb)
    if [ -n "${REMOTE_HOST:-}" ]; then
        ssh -o StrictHostKeyChecking=no "$REMOTE_HOST" \
            "/opt/run_md_setup.py '$pdb' '$OUT_DIR/$tag'"
    else
        /opt/run_md_setup.py "$pdb" "$OUT_DIR/$tag"
    fi
}
if [ -z "$HOSTS" ]; then
    for p in "${PDBS[@]}"; do run_one "$p" && PASS=$((PASS+1)) || FAIL=$((FAIL+1)); done
else
    IFS=',' read -r -a HARR <<< "$HOSTS"
    i=0
    for p in "${PDBS[@]}"; do
        h=${HARR[$(( i % ${#HARR[@]} ))]}
        REMOTE_HOST=$h run_one "$p" &
        i=$((i+1))
    done
    wait
    # Pass/fail tallied by presence of solvate output
    for p in "${PDBS[@]}"; do
        tag=$(basename "$p" .pdb)
        if ls "$OUT_DIR/$tag/"*_solvate.gro >/dev/null 2>&1; then
            PASS=$((PASS+1))
        else
            FAIL=$((FAIL+1))
        fi
    done
fi
echo "=== BATCH DONE: pass=$PASS fail=$FAIL of ${#PDBS[@]} ==="
[ $PASS -ge 1 ] && exit 0 || exit 1

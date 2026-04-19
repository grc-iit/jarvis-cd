#!/bin/bash
# Materialise a DeepDriveMD config from the template and run one
# pipeline experiment end-to-end. Uses /bin/echo placeholders for all
# four stages (see the template).
# Usage: run_ddmd.sh <experiment_dir> [<max_iter> [<num_tasks>]]
set -euo pipefail
EXP_DIR="${1:?experiment_dir required}"
MAX_ITER="${2:-1}"
NUM_TASKS="${3:-1}"

mkdir -p "$EXP_DIR"
CFG="$EXP_DIR/deepdrivemd.yaml"
sed \
    -e "s|__EXPERIMENT_DIR__|$EXP_DIR|g" \
    -e "s|__MAX_ITER__|$MAX_ITER|g" \
    -e "s|__NUM_TASKS__|$NUM_TASKS|g" \
    /opt/deepdrivemd.template.yaml > "$CFG"

# radical.entk writes sandboxes under $HOME/radical.pilot.sandbox by
# default; point it at the experiment dir so each run is self-contained.
export RADICAL_BASE="$EXP_DIR"
mkdir -p "$EXP_DIR/radical"

python -m deepdrivemd.deepdrivemd -c "$CFG"

# Success: experiment dir populated with at least one stage sub-dir.
if find "$EXP_DIR" -type d -name 'stage*' -print -quit | grep -q .; then
    echo "=== SUCCESS: DeepDriveMD pipeline completed under $EXP_DIR ==="
    exit 0
fi
echo "FAIL: no stage output dirs under $EXP_DIR"
exit 1

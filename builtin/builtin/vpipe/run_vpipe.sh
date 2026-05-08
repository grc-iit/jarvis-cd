#!/bin/bash
# Initialise a V-pipe project and run Snakemake with --use-conda.
# Usage: run_vpipe.sh <virus> <project_dir> [<extra snakemake flags>...]
#   virus:        one of sars-cov-2, hiv (must exist under /opt/V-pipe/tests/data/)
#   project_dir:  working directory for this run (will be created)
set -euo pipefail
VIRUS="${1:-sars-cov-2}"
PROJECT="${2:-/output/vpipe-run}"
shift 2 || true

TESTDATA=/opt/V-pipe/tests/data/${VIRUS}
if [ ! -d "$TESTDATA" ]; then
    echo "FAIL: $TESTDATA missing"
    exit 1
fi

mkdir -p "$PROJECT"
cd "$PROJECT"

# init_project writes a config/ dir and a vpipe wrapper; symlink in the
# upstream test data so the default samples.tsv resolves to real FASTQs.
"/opt/V-pipe/init_project.sh"
ln -sfT "$TESTDATA" samples

CORES="${CORES:-2}"
snakemake -s /opt/V-pipe/workflow/Snakefile \
    --cores "$CORES" \
    --use-conda \
    --conda-prefix /opt/conda/envs/vpipe-tools \
    --rerun-triggers mtime \
    "$@"

# Success: at least one output under results/
if find results -type f 2>/dev/null | grep -q .; then
    echo "=== SUCCESS: $VIRUS run produced files under $PROJECT/results ==="
    exit 0
fi
echo "FAIL: no results/ output under $PROJECT"
exit 1

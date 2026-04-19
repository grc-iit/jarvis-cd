#!/bin/bash
# Run the rna-seq-star-deseq2 pipeline on the bundled S. cerevisiae
# benchmark (or on user data mounted to the same layout).
# Usage: run_rnaseq.sh <workdir>
set -euo pipefail
WORKDIR="${1:?workdir required}"
mkdir -p "$WORKDIR"

# Stage config + samples/units + FASTQs. If the workdir already has a
# samples.tsv the user supplied, leave it alone.
if [ ! -f "$WORKDIR/samples.tsv" ]; then
    cp /opt/rnaseq-bench/samples.tsv "$WORKDIR/"
    cp /opt/rnaseq-bench/units.tsv   "$WORKDIR/"
    cp /opt/rnaseq-bench/config.yaml "$WORKDIR/"
    # Point ngs-test-data at the bundled absolute path and the .tsv files at workdir.
    sed -i "s|ngs-test-data/|/opt/rnaseq-bench/ngs-test-data/|g"   "$WORKDIR/units.tsv"
    sed -i "s|config_basic/samples.tsv|$WORKDIR/samples.tsv|"      "$WORKDIR/config.yaml"
    sed -i "s|config_basic/units.tsv|$WORKDIR/units.tsv|"          "$WORKDIR/config.yaml"
fi

CORES="${CORES:-2}"
cd "$WORKDIR"
snakemake \
    --snakefile /opt/rna-seq-star-deseq2/workflow/Snakefile \
    --configfile "$WORKDIR/config.yaml" \
    --cores "$CORES" \
    --use-conda \
    --conda-frontend conda \
    --conda-prefix /opt/conda/envs/rnaseq-tools \
    --directory "$WORKDIR" \
    --rerun-triggers mtime \
    --latency-wait 30

# Success: per-sample STAR outputs + diffexp table written.
if find "$WORKDIR/results/diffexp" -name '*.diffexp.symbol.tsv' 2>/dev/null | grep -q . \
   && find "$WORKDIR/results/star"     -maxdepth 2 -name 'Aligned*.bam' 2>/dev/null | grep -q .; then
    echo "=== SUCCESS: diffexp table + STAR alignments produced under $WORKDIR/results ==="
    exit 0
fi
echo "FAIL: expected output files not found under $WORKDIR/results"
exit 1

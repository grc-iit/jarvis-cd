#!/bin/bash
# Run the rna-seq-star-deseq2 pipeline on the bundled S. cerevisiae
# benchmark (or on user data mounted to the same layout), exercising the
# CTE FUSE adapter for durable outputs.
#
# Usage: run_rnaseq.sh [<out_dir>]
#   out_dir — where to stage final results; intermediates live on /tmp.
#             Default: /mnt/wrp_cte/rnaseq_out (the CTE FUSE mount).
set -euo pipefail
OUT_DIR="${1:-/mnt/wrp_cte/rnaseq_out}"

# jarvis's docker-exec env does not inherit the image's ENV PATH, and
# jarvis's pipeline-merge drops ENV from non-first packages anyway, so
# prepend the snakemake env + /opt/conda/bin explicitly here.
export PATH="/opt/rnaseq-env/bin:/opt/conda/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
export PYTHONNOUSERSITE=1

# Snakemake writes many intermediates and uses atomic-write-then-rename
# for metadata (.snakemake/), shell-rule outputs, and per-rule temp
# files. wrp_cte_fuse returns ENOSYS on rename(2) (same constraint
# documented in metagem_cte / montage_cte), so run the workflow in a
# /tmp scratch dir (where rename works), then stage the final results
# tree into the FUSE mount via plain cp — which is what exercises the
# CTE adapter.
WORKDIR="${RNASEQ_SCRATCH_DIR:-/tmp/rnaseq-scratch}"
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"

# Stage config + samples/units + FASTQs. Point ngs-test-data at the
# bundled absolute path and the .tsv files at the scratch workdir.
cp /opt/rnaseq-bench/samples.tsv "$WORKDIR/"
cp /opt/rnaseq-bench/units.tsv   "$WORKDIR/"
cp /opt/rnaseq-bench/config.yaml "$WORKDIR/"
sed -i "s|ngs-test-data/|/opt/rnaseq-bench/ngs-test-data/|g"   "$WORKDIR/units.tsv"
sed -i "s|config_basic/samples.tsv|$WORKDIR/samples.tsv|"      "$WORKDIR/config.yaml"
sed -i "s|config_basic/units.tsv|$WORKDIR/units.tsv|"          "$WORKDIR/config.yaml"

CORES="${CORES:-2}"
cd "$WORKDIR"
# Skip gene_2_symbol: that rule queries Ensembl BioMart (external MySQL
# server + archive listing) to annotate DESeq2 output with gene symbols.
# Ensembl's public BioMart is flaky/unreachable from many networks and
# periodically drops older release databases, which turns the smoke test
# into a network-liveness check. The rule is pure post-processing —
# cutadapt, STAR, featureCounts, and DESeq2 all run before it and
# produce the substantive RNA-seq outputs the pipeline is meant to
# exercise. --omit-from drops it (and its downstream) from the DAG.
snakemake \
    --snakefile /opt/rna-seq-star-deseq2/workflow/Snakefile \
    --configfile "$WORKDIR/config.yaml" \
    --cores "$CORES" \
    --use-conda \
    --conda-frontend conda \
    --conda-prefix /opt/conda/envs/rnaseq-tools \
    --directory "$WORKDIR" \
    --rerun-triggers mtime \
    --latency-wait 30 \
    --omit-from gene_2_symbol

# Verify per-sample STAR outputs + DESeq2 diffexp table in scratch. Do
# not require .diffexp.symbol.tsv — that's produced by gene_2_symbol,
# which we intentionally skip (see above).
if ! find "$WORKDIR/results/diffexp" -name '*.diffexp.tsv' 2>/dev/null | grep -q . \
   || ! find "$WORKDIR/results/star" -maxdepth 2 -name 'Aligned*.bam' 2>/dev/null | grep -q .; then
    echo "FAIL: expected output files not found under $WORKDIR/results"
    exit 1
fi

# Stage results tree into the FUSE mount. cp is open+write+close (no
# rename), which wrp_cte_fuse supports. This is what the wrp_cte_libfuse
# adapter sees — STAR BAM/SAM files, DESeq2 TSVs, and QC reports.
echo "--- Staging results to $OUT_DIR ---"
mkdir -p "$OUT_DIR"
cp -r "$WORKDIR/results/." "$OUT_DIR/"
# Also copy the config and samples metadata for traceability.
cp "$WORKDIR"/*.tsv "$WORKDIR"/*.yaml "$OUT_DIR/" 2>/dev/null || true

CTE_COUNT=$(find "$OUT_DIR" -type f 2>/dev/null | wc -l)
CTE_BYTES=$(find "$OUT_DIR" -type f -printf '%s\n' 2>/dev/null | awk 'BEGIN{s=0}{s+=$1}END{print s}')
echo "=== SUCCESS: diffexp table + STAR alignments produced under $OUT_DIR ==="
echo "=== CTE FUSE traffic: $CTE_COUNT files, $CTE_BYTES bytes under $OUT_DIR ==="
exit 0

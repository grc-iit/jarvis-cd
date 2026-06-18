#!/bin/bash
# Run the rna-seq-star-deseq2 snakemake pipeline on the bundled
# S. cerevisiae benchmark. The whole DAG (cutadapt → STAR index →
# STAR align → samtools → featureCounts → DESeq2 → …) runs directly
# in $WORKDIR; each rule reads upstream rule outputs and writes new
# files back into $WORKDIR. The storage layer under $WORKDIR sees the
# full producer-consumer chain, not just a final bulk cp.
#
# (Earlier revisions detoured intermediates through /tmp/rnaseq-scratch
# because wrp_cte_fuse returned ENOSYS on rename(2); snakemake's atomic-
# write-then-rename metadata, shell-rule outputs, and per-rule temp
# files then broke. Backends that implement rename(2) correctly — NFS,
# dumbwarp, ext4 — don't need that detour and shouldn't pay its cost
# in lost producer-consumer traffic.)
#
# Usage: run_rnaseq.sh [<workdir>]
#   workdir — where snakemake stages config + runs the DAG; results
#             land at $workdir/results.
#             Default: ${HOME}/rnaseq_out
set -euo pipefail
WORKDIR="${1:-${HOME}/rnaseq_out}"

# jarvis's docker-exec env does not inherit the image's ENV PATH, and
# jarvis's pipeline-merge drops ENV from non-first packages anyway, so
# prepend the snakemake env + /opt/conda/bin explicitly here.
export PATH="/opt/rnaseq-env/bin:/opt/conda/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
export PYTHONNOUSERSITE=1

mkdir -p "$WORKDIR"

# Stage config + samples/units + FASTQs. Point ngs-test-data at the
# bundled absolute path and the .tsv files at $WORKDIR. Use cat + redirect
# (not cp + sed -i) so each rep writes a fresh, fully-resolved config —
# sed -i's rename-in-place dance has misbehaved when $WORKDIR is on a
# FUSE mount during dumbwarp early-mount races.
sed "s|ngs-test-data/|/opt/rnaseq-bench/ngs-test-data/|g" \
    /opt/rnaseq-bench/units.tsv > "$WORKDIR/units.tsv"
cp /opt/rnaseq-bench/samples.tsv "$WORKDIR/samples.tsv"
sed -e "s|config_basic/samples.tsv|$WORKDIR/samples.tsv|" \
    -e "s|config_basic/units.tsv|$WORKDIR/units.tsv|" \
    /opt/rnaseq-bench/config.yaml > "$WORKDIR/config.yaml"
# Sanity check — fail loudly if the substitutions didn't land.
if grep -q "^samples: config_basic" "$WORKDIR/config.yaml"; then
    echo "FAIL: config.yaml substitution did not take effect:" >&2
    head -5 "$WORKDIR/config.yaml" >&2
    exit 1
fi

CORES="${CORES:-2}"
# Per-WORKDIR tmpdir so concurrent reps on the same host don't race on
# /var/tmp/snakemake*'s lockfile. Snakemake honors $TMPDIR for its
# internal scratch dir, not the rule outputs.
export TMPDIR="$WORKDIR/.tmp"
mkdir -p "$TMPDIR"
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

# Verify per-sample STAR outputs + DESeq2 diffexp table.
if ! find "$WORKDIR/results/diffexp" -name '*.diffexp.tsv' 2>/dev/null | grep -q . \
   || ! find "$WORKDIR/results/star" -maxdepth 2 -name 'Aligned*.bam' 2>/dev/null | grep -q .; then
    echo "FAIL: expected output files not found under $WORKDIR/results"
    exit 1
fi

OUT_COUNT=$(find "$WORKDIR" -type f 2>/dev/null | wc -l)
OUT_BYTES=$(find "$WORKDIR" -type f -printf '%s\n' 2>/dev/null | awk 'BEGIN{s=0}{s+=$1}END{print s}')
echo "=== SUCCESS: diffexp table + STAR alignments produced under $WORKDIR/results ==="
echo "=== Pipeline I/O traffic: $OUT_COUNT files, $OUT_BYTES bytes under $WORKDIR ==="
exit 0

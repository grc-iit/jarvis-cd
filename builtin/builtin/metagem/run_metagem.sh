#!/bin/bash
# Set up a metaGEM working directory and run the qfilter target.
# Usage: run_metagem.sh <workdir> [<snakemake extra flags>...]
#
# The caller is expected to have placed paired-end FASTQ files as
# <workdir>/dataset/<sampleID>/<sampleID>_R1.fastq.gz (and _R2). If
# <workdir>/dataset is empty, the script falls back to metaGEM's
# upstream downloadToy rule to fetch a small toy dataset.
set -euo pipefail
WORKDIR="${1:?workdir required}"
shift || true

mkdir -p "$WORKDIR"/{dataset,scratch,scripts,qfiltered}

# Rewrite config paths to point at $WORKDIR (upstream config has
# /path/to/project/... placeholders).
sed -E \
    -e "s|root: .*|root: $WORKDIR|" \
    -e "s|scratch: .*|scratch: $WORKDIR/scratch|" \
    /opt/metaGEM/config/config.yaml > "$WORKDIR/config.yaml"

# metaGEM expects helper scripts in <root>/<folder.scripts>/.
cp -rn /opt/metaGEM/workflow/scripts/. "$WORKDIR/scripts/" 2>/dev/null || true

# Activate the metagem env (fastp) so Snakefile rules can see it.
source /opt/conda/etc/profile.d/conda.sh

# If no dataset was provided, trigger metaGEM's own downloader.
if ! ls "$WORKDIR/dataset"/*/*_R1.fastq.gz >/dev/null 2>&1; then
    echo "No FASTQs under $WORKDIR/dataset — invoking downloadToy..."
    snakemake -s /opt/metaGEM/workflow/Snakefile \
        --configfile "$WORKDIR/config.yaml" \
        --cores 1 downloadToy
fi

CORES="${CORES:-2}"
snakemake -s /opt/metaGEM/workflow/Snakefile \
    --configfile "$WORKDIR/config.yaml" \
    --cores "$CORES" \
    --rerun-triggers mtime \
    qfilter \
    "$@"

if find "$WORKDIR/qfiltered" -name '*_R1.fastq.gz' 2>/dev/null | grep -q .; then
    echo "=== SUCCESS: qfilter produced filtered reads under $WORKDIR/qfiltered ==="
    exit 0
fi
echo "FAIL: no qfiltered reads produced under $WORKDIR"
exit 1

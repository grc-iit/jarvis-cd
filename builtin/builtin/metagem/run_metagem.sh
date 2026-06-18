#!/bin/bash
# Set up a metaGEM working directory and run the qfilter target.
# All intermediates flow through $WORKDIR — dataset staging, the
# snakemake scratch dir, and fastp's qfilter outputs. Subsequent
# pipeline stages read upstream outputs back from $WORKDIR. The
# storage layer under $WORKDIR sees the full producer-consumer
# chain, not just a final bulk cp.
#
# (Earlier revisions detoured fastp's outputs through /tmp/metagem-
# qfiltered because wrp_cte_fuse returned ENOSYS on rename(2) and
# choked on parallel writes; backends that implement rename(2)
# correctly — NFS, dumbwarp, ext4 — don't need that detour and
# shouldn't pay its cost in lost producer-consumer traffic.)
#
# Usage: run_metagem.sh <workdir> [<snakemake extra flags>...]
#
# The caller is expected to have placed paired-end FASTQ files as
# <workdir>/dataset/<sampleID>/<sampleID>_R1.fastq.gz (and _R2). If
# <workdir>/dataset is empty, the script either stages from the
# pre-baked cache pointed at by $METAGEM_STAGE_DIR (no network) or
# falls back to metaGEM's downloadToy fetch.
set -euo pipefail
WORKDIR="${1:?workdir required}"
shift || true

mkdir -p "$WORKDIR"/{dataset,scripts,qfiltered,scratch}

# Snakemake scratch goes under $WORKDIR so the producer-consumer
# chain stays on the user's storage. Override via METAGEM_SCRATCH_DIR
# for benchmarks that explicitly want to compare on-storage vs. off-
# storage scratch.
SCRATCH_DIR="${METAGEM_SCRATCH_DIR:-$WORKDIR/scratch}"
mkdir -p "$SCRATCH_DIR"

# Rewrite config paths to point at $WORKDIR (upstream config has
# /path/to/project/... placeholders). Also point envs.metagem at the
# bare conda env name so `conda activate metagem` resolves against
# /opt/conda/envs/metagem (upstream default 'envs/metagem' is a yaml
# file path for --use-conda, which we don't use).
sed -E \
    -e "s|root: .*|root: $WORKDIR|" \
    -e "s|scratch: .*|scratch: $SCRATCH_DIR|" \
    -e "s|^([[:space:]]*metagem:)[[:space:]]*.*|\1 metagem|" \
    /opt/metaGEM/config/config.yaml > "$WORKDIR/config.yaml"

# metaGEM expects helper scripts in <root>/<folder.scripts>/.
cp -rn /opt/metaGEM/workflow/scripts/. "$WORKDIR/scripts/" 2>/dev/null || true

# Source conda init (needed so `conda activate` works below).
source /opt/conda/etc/profile.d/conda.sh

# jarvis's docker-exec env does not inherit the image's ENV PATH, so
# snakemake (/opt/metagem-env/bin) and fastp (/opt/conda/envs/metagem/bin)
# are not on PATH by default. Prepend both, and also pre-activate the
# fastp env so Snakefile `shell:` rules that invoke fastp resolve it.
export PATH="/opt/metagem-env/bin:/opt/conda/envs/metagem/bin:/opt/conda/bin:${PATH}"

# Stage the toy dataset into $WORKDIR/dataset. Prefer a pre-baked cache
# at $METAGEM_STAGE_DIR (no network); fall back to wget from the
# upstream toy URL list.
if ! ls "$WORKDIR/dataset"/*/*_R1.fastq.gz >/dev/null 2>&1; then
    STAGE_DIR="${METAGEM_STAGE_DIR:-/tmp/metagem-stage}"
    if [ -f "$STAGE_DIR/.staged" ]; then
        echo "Staging $STAGE_DIR -> $WORKDIR/dataset (pre-baked cache)..."
        for d in "$STAGE_DIR"/*/; do
            [ -d "$d" ] && cp -r "$d" "$WORKDIR/dataset/"
        done
    else
        echo "No FASTQs under $WORKDIR/dataset and no pre-baked cache at $STAGE_DIR — fetching..."
        rm -rf "$STAGE_DIR"
        mkdir -p "$STAGE_DIR"
        (
            cd "$STAGE_DIR"
            while IFS= read -r url; do
                [ -z "$url" ] && continue
                fname="$(basename "$url" | sed -e 's/?download=1//g' -e 's/_/_R/g')"
                [ -f "$fname" ] || wget -q --show-progress -O "$fname" "$url"
            done < /opt/metaGEM/workflow/scripts/download_toydata.txt
            for f in *.gz; do
                [ -e "$f" ] || continue
                sid="${f%%_R*}"
                mkdir -p "$sid"
                mv "$f" "$sid/"
            done
        )
        touch "$STAGE_DIR/.staged"
        for d in "$STAGE_DIR"/*/; do
            [ -d "$d" ] && cp -r "$d" "$WORKDIR/dataset/"
        done
    fi
fi

CORES="${CORES:-2}"

# Run fastp directly per sample, writing outputs straight into $WORKDIR/
# qfiltered. This is the same I/O pattern metaGEM's qfilter rule produces
# (paired-end input read → filtered paired output write + json/html
# report); using fastp directly avoids the snakemake `mv` dance whose
# brittleness is unrelated to the workflow's actual data flow.
FASTP="/opt/conda/envs/metagem/bin/fastp"
if [ ! -x "$FASTP" ]; then
    echo "FAIL: fastp not found at $FASTP" >&2
    exit 1
fi

ran_any=0
for sample_dir in "$WORKDIR/dataset"/*/; do
    [ -d "$sample_dir" ] || continue
    sid="$(basename "$sample_dir")"
    in_r1="$sample_dir/${sid}_R1.fastq.gz"
    in_r2="$sample_dir/${sid}_R2.fastq.gz"
    if [ ! -f "$in_r1" ] || [ ! -f "$in_r2" ]; then
        echo "SKIP $sid: missing R1/R2 under $sample_dir" >&2
        continue
    fi
    dst_sd="$WORKDIR/qfiltered/$sid"; mkdir -p "$dst_sd"
    echo "=== fastp $sid ==="
    "$FASTP" --thread "$CORES" \
        -i "$in_r1" -I "$in_r2" \
        -o "$dst_sd/${sid}_R1.fastq.gz" \
        -O "$dst_sd/${sid}_R2.fastq.gz" \
        -j "$dst_sd/${sid}.json" \
        -h "$dst_sd/${sid}.html"
    ran_any=1
done

if [ "$ran_any" -eq 0 ]; then
    echo "FAIL: no sample subdirs with R1+R2 under $WORKDIR/dataset" >&2
    exit 1
fi

OUT_COUNT=$(find "$WORKDIR" -type f 2>/dev/null | wc -l)
OUT_BYTES=$(find "$WORKDIR" -type f -printf '%s\n' 2>/dev/null | awk 'BEGIN{s=0}{s+=$1}END{print s}')
echo "=== SUCCESS: qfilter produced filtered reads under $WORKDIR/qfiltered ==="
echo "=== Pipeline I/O traffic: $OUT_COUNT files, $OUT_BYTES bytes under $WORKDIR ==="
exit 0

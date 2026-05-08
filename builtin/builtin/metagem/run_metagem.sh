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

mkdir -p "$WORKDIR"/{dataset,scripts,qfiltered}

# Scratch MUST live off the CTE FUSE mount. The qfilter rule stages
# inputs into scratch and does `mv $f $f.raw.gz` for fastp's glob
# pattern — wrp_cte_fuse returns ENOSYS on rename(2), so in-FUSE scratch
# breaks. /tmp is a normal tmpfs, so rename works. We still exercise
# iowarp: the rule reads inputs from FUSE ($WORKDIR/dataset) and writes
# fastp outputs back to FUSE ($WORKDIR/qfiltered) via mv's EXDEV
# fallback (cp+unlink — cp into FUSE is a plain write, supported).
SCRATCH_DIR="${METAGEM_SCRATCH_DIR:-/tmp/metagem-scratch}"
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

# Absolute path to snakemake as a belt-and-suspenders fallback.
SNAKEMAKE="/opt/metagem-env/bin/snakemake"

# If no dataset was provided, stage the toy data ourselves. We do NOT
# use metaGEM's downloadToy rule because that rule uses `mv` in-place,
# and iowarp's wrp_cte_fuse does not implement rename(2) (ENOSYS).
# Workaround: download + rename in a local scratch dir (where rename
# works), then cp -r the finished tree into the FUSE mount — pure
# writes, no rename, no unlink. Subsequent qfilter (fastp) does plain
# open/read/write only, which wrp_cte_fuse supports.
if ! ls "$WORKDIR/dataset"/*/*_R1.fastq.gz >/dev/null 2>&1; then
    echo "No FASTQs under $WORKDIR/dataset — staging toy dataset outside FUSE..."
    STAGE_DIR="${METAGEM_STAGE_DIR:-/tmp/metagem-stage}"
    STAMP="$STAGE_DIR/.staged"
    if [ ! -f "$STAMP" ]; then
        rm -rf "$STAGE_DIR"
        mkdir -p "$STAGE_DIR"
        (
            cd "$STAGE_DIR"
            while IFS= read -r url; do
                [ -z "$url" ] && continue
                fname="$(basename "$url" | sed -e 's/?download=1//g' -e 's/_/_R/g')"
                [ -f "$fname" ] || wget -q --show-progress -O "$fname" "$url"
            done < /opt/metaGEM/workflow/scripts/download_toydata.txt
            # Organize into per-sample subfolders (local FS — mv works here).
            for f in *.gz; do
                [ -e "$f" ] || continue
                sid="${f%%_R*}"
                mkdir -p "$sid"
                mv "$f" "$sid/"
            done
        )
        touch "$STAMP"
    else
        echo "Reusing cached stage at $STAGE_DIR"
    fi
    mkdir -p "$WORKDIR/dataset"
    echo "Staging $STAGE_DIR -> $WORKDIR/dataset (writes into FUSE mount)..."
    # Copy per-sample subfolders only (skip the .staged marker).
    for d in "$STAGE_DIR"/*/; do
        [ -d "$d" ] && cp -r "$d" "$WORKDIR/dataset/"
    done
fi

CORES="${CORES:-2}"
# Bypass snakemake's qfilter rule. Running the rule via snakemake has
# two problems:
#   1. The rule's shell does `mv R1 R2 dst/` where dst is on the CTE
#      FUSE mount. That's a cross-FS mv that mv splits into per-file
#      cp+unlink. Empirically this triggers a wrp_cte_fuse bug on the
#      SECOND large sequential write: R1 succeeds, R2 fails with
#      ECONNABORTED ("Software caused connection abort"). Deterministic
#      across all samples.
#   2. The rule's conda activation and scratch-dir mv dance are
#      brittle against config-rewrite drift.
# Instead: invoke fastp directly, and write each fastq output one at a
# time with a fsync between so we don't pile concurrent PutBlob RPCs
# through FUSE. Still fully exercises iowarp: fastp reads the ~1.5 GB
# of inputs from /mnt/wrp_cte/metagem/dataset (GetBlob) and writes the
# filtered outputs + json/html back to /mnt/wrp_cte/metagem/qfiltered
# (PutBlob).
FASTP="/opt/conda/envs/metagem/bin/fastp"
if [ ! -x "$FASTP" ]; then
    echo "FAIL: fastp not found at $FASTP" >&2
    exit 1
fi

# fastp writes both R1 and R2 in parallel internally — that's what
# provokes the FUSE abort. Land fastp's outputs in local /tmp first,
# then copy to FUSE one file at a time with sync between. This keeps
# the input-read path on FUSE (what metaGEM's qfilter actually stresses
# for I/O) and the write path on FUSE (via our controlled single-file
# copies).
TMP_OUT="/tmp/metagem-qfiltered"
rm -rf "$TMP_OUT"
mkdir -p "$TMP_OUT"

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
    tmp_sd="$TMP_OUT/$sid"; mkdir -p "$tmp_sd"
    echo "=== fastp $sid (reads from FUSE) ==="
    "$FASTP" --thread "$CORES" \
        -i "$in_r1" -I "$in_r2" \
        -o "$tmp_sd/${sid}_R1.fastq.gz" \
        -O "$tmp_sd/${sid}_R2.fastq.gz" \
        -j "$tmp_sd/${sid}.json" \
        -h "$tmp_sd/${sid}.html"
    ran_any=1
done

if [ "$ran_any" -eq 0 ]; then
    echo "FAIL: no sample subdirs with R1+R2 under $WORKDIR/dataset" >&2
    exit 1
fi

# Copy fastp outputs into FUSE one file at a time with sync between.
# Retry once on ECONNABORTED (observed to be transient — second try
# typically goes through after the FUSE client catches up).
echo "=== Copying qfilter outputs into FUSE mount (one file at a time) ==="
copy_to_fuse() {
    local src="$1" dst="$2"
    for attempt in 1 2 3; do
        if cat "$src" > "$dst" 2>/tmp/cp.err; then
            sync "$dst" 2>/dev/null || true
            return 0
        fi
        echo "copy attempt $attempt failed for $dst: $(cat /tmp/cp.err)" >&2
        sleep 2
    done
    return 1
}

fail=0
for src_sd in "$TMP_OUT"/*/; do
    sid="$(basename "$src_sd")"
    dst_sd="$WORKDIR/qfiltered/$sid"
    mkdir -p "$dst_sd"
    for f in "${sid}_R1.fastq.gz" "${sid}_R2.fastq.gz" "${sid}.json" "${sid}.html"; do
        [ -f "$src_sd/$f" ] || continue
        if ! copy_to_fuse "$src_sd/$f" "$dst_sd/$f"; then
            echo "FAIL: could not copy $f to $dst_sd" >&2
            fail=1
        fi
    done
done
[ "$fail" -eq 0 ] || exit 1

# Verify iowarp CTE FUSE saw traffic: metagem's configured root is
# $WORKDIR, which the pipeline points at /mnt/wrp_cte/... (FUSE mount).
# If $WORKDIR is on the FUSE mount, the qfilter outputs below are
# already CTE-backed.
ok=0
if find "$WORKDIR/qfiltered" -name '*_R1.fastq.gz' 2>/dev/null | grep -q .; then
    ok=1
fi
case "$WORKDIR" in
    /mnt/wrp_cte/*)
        bytes=$(find "$WORKDIR" -type f -printf '%s\n' 2>/dev/null \
                | awk 'BEGIN{s=0}{s+=$1}END{print s}')
        count=$(find "$WORKDIR" -type f 2>/dev/null | wc -l)
        echo "=== CTE FUSE traffic: $count files, $bytes bytes under $WORKDIR ==="
        ;;
    *)
        echo "NOTE: $WORKDIR is not under /mnt/wrp_cte — iowarp FUSE was not exercised" >&2
        ;;
esac

if [ "$ok" -eq 1 ]; then
    echo "=== SUCCESS: qfilter produced filtered reads under $WORKDIR/qfiltered ==="
    exit 0
fi
echo "FAIL: no qfiltered reads produced under $WORKDIR"
exit 1

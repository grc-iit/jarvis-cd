#!/bin/bash
set -euo pipefail

# Defaults point at the benchmark region pre-staged during the container
# build (/opt/montage-bench) and the CTE FUSE mountpoint for durable
# outputs, so `/opt/run_mosaic.sh` with no args exercises the pipeline
# end-to-end.
RAW_DIR="${1:-/opt/montage-bench/raw_images}"
HDR="${2:-/opt/montage-bench/region.hdr}"
OUT="${3:-/mnt/wrp_cte/montage_out}"
HOSTS="${4:-}"

# Montage binaries chain ~10 stages via intermediate *.tbl / *.fits files
# and use atomic-write-then-rename internally. wrp_cte_fuse returns
# ENOSYS on rename(2) (same limitation documented in metagem_cte's
# run_metagem.sh), so in-FUSE intermediates break — the rename silently
# fails, the follow-on stage opens an empty file, and mProjExec reports
# "Need column fname in input". Run the whole pipeline in a scratch dir
# on /tmp (where rename works), then stage the durable outputs into the
# FUSE mountpoint via plain writes — which is what actually exercises
# the wrp_cte_libfuse adapter (cp into FUSE is a normal write, the
# pattern deepdrivemd_cte uses).
SCRATCH="${MONTAGE_SCRATCH_DIR:-/tmp/montage-scratch}"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"/{projected,diffs,corrected}

# If the build-time 2MASS download failed (network unreachable from the
# build container), the raw-images dir will be empty. Retry the fetch at
# runtime — the deploy container has outbound networking and IRSA may be
# reachable now. Bounded so a persistent outage fails fast with a clear
# message instead of hanging the pipeline start.
if [ ! -s "$HDR" ] || [ -z "$(ls -A "$RAW_DIR" 2>/dev/null)" ]; then
    echo "Benchmark region not pre-staged; fetching M17 J-band at runtime..."
    mkdir -p "$RAW_DIR"
    cd "$(dirname "$HDR")"
    mHdr "M17" 0.2 "$(basename "$HDR")"
    mArchiveList 2mass J "M17" 0.2 0.2 remote.tbl
    if ! timeout 600 mArchiveExec -p "$RAW_DIR" remote.tbl; then
        echo "ERROR: cannot fetch 2MASS benchmark images (IRSA archive unreachable)" >&2
        echo "       supply raw FITS dir + region.hdr as arg 1 and 2 to /opt/run_mosaic.sh" >&2
        exit 1
    fi
    cd -
fi

echo "--- 1. mImgtbl ---";      mImgtbl "$RAW_DIR" "$SCRATCH/images.tbl"
echo "--- 2. mProjExec ---"
if [ -n "$HOSTS" ]; then
    IFS=',' read -r -a A <<< "$HOSTS"; N=${#A[@]}
    # Montage tables have variable-length headers: lines starting with '\' or '|'.
    awk '/^[\\|]/'  "$SCRATCH/images.tbl" > "$SCRATCH/images.header"
    awk '!/^[\\|]/' "$SCRATCH/images.tbl" > "$SCRATCH/images.body"
    split -n l/$N -d "$SCRATCH/images.body" "$SCRATCH/imgpart_"
    i=0
    for h in "${A[@]}"; do
        cat "$SCRATCH/images.header" "$SCRATCH/imgpart_$(printf '%02d' $i)" \
            > "$SCRATCH/imgtbl_${i}.tbl"
        ssh -o StrictHostKeyChecking=no "$h" \
            "mProjExec -p '$RAW_DIR' '$SCRATCH/imgtbl_${i}.tbl' '$HDR' '$SCRATCH/projected' '$SCRATCH/stats_${i}.tbl'" &
        i=$((i+1))
    done
    wait
    cat "$SCRATCH/stats_"*.tbl > "$SCRATCH/stats.tbl"
else
    mProjExec -p "$RAW_DIR" "$SCRATCH/images.tbl" "$HDR" "$SCRATCH/projected" "$SCRATCH/stats.tbl"
fi
echo "--- 3. mImgtbl (projected) ---"; mImgtbl "$SCRATCH/projected" "$SCRATCH/proj_images.tbl"
echo "--- 4. mOverlaps ---";   mOverlaps "$SCRATCH/proj_images.tbl" "$SCRATCH/diffs.tbl"
echo "--- 5. mDiffExec ---";   mDiffExec -p "$SCRATCH/projected" "$SCRATCH/diffs.tbl" "$HDR" "$SCRATCH/diffs"
echo "--- 6. mFitExec ---";    mFitExec "$SCRATCH/diffs.tbl" "$SCRATCH/fits.tbl" "$SCRATCH/diffs"
echo "--- 7. mBgModel ---";    mBgModel "$SCRATCH/proj_images.tbl" "$SCRATCH/fits.tbl" "$SCRATCH/corrections.tbl"
echo "--- 8. mBgExec ---";     mBgExec -p "$SCRATCH/projected" "$SCRATCH/proj_images.tbl" "$SCRATCH/corrections.tbl" "$SCRATCH/corrected"
echo "--- 9. mImgtbl (corrected) ---"; mImgtbl "$SCRATCH/corrected" "$SCRATCH/corr_images.tbl"
echo "--- 10. mAdd ---";       mAdd -p "$SCRATCH/corrected" "$SCRATCH/corr_images.tbl" "$HDR" "$SCRATCH/mosaic.fits"

# Stage durable outputs into the CTE FUSE mountpoint. `cp` into FUSE is
# a plain open+write+close (no rename), so wrp_cte_fuse handles it. This
# is the phase that actually exercises the CTE adapter with real FITS
# payloads — the .tbl indexes, per-tile projected/corrected FITS, and
# the final mosaic.fits.
echo "--- Staging results to $OUT ---"
mkdir -p "$OUT"/{projected,corrected}
cp "$SCRATCH"/*.tbl           "$OUT/"
cp "$SCRATCH"/mosaic.fits     "$OUT/"
cp "$SCRATCH"/projected/*.fits "$OUT/projected/" 2>/dev/null || true
cp "$SCRATCH"/corrected/*.fits "$OUT/corrected/" 2>/dev/null || true

[ -s "$OUT/mosaic.fits" ] && echo "=== SUCCESS: mosaic.fits $(stat --printf='%s' "$OUT/mosaic.fits") bytes ==="
CTE_COUNT=$(find "$OUT" -type f 2>/dev/null | wc -l)
CTE_BYTES=$(find "$OUT" -type f -printf '%s\n' 2>/dev/null | awk 'BEGIN{s=0}{s+=$1}END{print s}')
echo "=== CTE FUSE traffic: $CTE_COUNT files, $CTE_BYTES bytes under $OUT ==="

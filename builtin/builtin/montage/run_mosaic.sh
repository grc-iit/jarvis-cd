#!/bin/bash
set -euo pipefail
RAW_DIR="${1:?raw_dir required}"
HDR="${2:?region.hdr required}"
OUT="${3:?out_dir required}"
HOSTS="${4:-}"
mkdir -p "$OUT"/{projected,diffs,corrected}
echo "--- 1. mImgtbl ---";      mImgtbl "$RAW_DIR" "$OUT/images.tbl"
echo "--- 2. mProjExec ---"
if [ -n "$HOSTS" ]; then
    IFS=',' read -r -a A <<< "$HOSTS"; N=${#A[@]}
    # Montage tables have variable-length headers: lines starting with '\' or '|'.
    awk '/^[\\|]/'  "$OUT/images.tbl" > "$OUT/images.header"
    awk '!/^[\\|]/' "$OUT/images.tbl" > "$OUT/images.body"
    split -n l/$N -d "$OUT/images.body" "$OUT/imgpart_"
    i=0
    for h in "${A[@]}"; do
        cat "$OUT/images.header" "$OUT/imgpart_$(printf '%02d' $i)" \
            > "$OUT/imgtbl_${i}.tbl"
        ssh -o StrictHostKeyChecking=no "$h" \
            "mProjExec -p '$RAW_DIR' '$OUT/imgtbl_${i}.tbl' '$HDR' '$OUT/projected' '$OUT/stats_${i}.tbl'" &
        i=$((i+1))
    done
    wait
    cat "$OUT/stats_"*.tbl > "$OUT/stats.tbl"
else
    mProjExec -p "$RAW_DIR" "$OUT/images.tbl" "$HDR" "$OUT/projected" "$OUT/stats.tbl"
fi
echo "--- 3. mImgtbl (projected) ---"; mImgtbl "$OUT/projected" "$OUT/proj_images.tbl"
echo "--- 4. mOverlaps ---";   mOverlaps "$OUT/proj_images.tbl" "$OUT/diffs.tbl"
echo "--- 5. mDiffExec ---";   mDiffExec -p "$OUT/projected" "$OUT/diffs.tbl" "$HDR" "$OUT/diffs"
echo "--- 6. mFitExec ---";    mFitExec "$OUT/diffs.tbl" "$OUT/fits.tbl" "$OUT/diffs"
echo "--- 7. mBgModel ---";    mBgModel "$OUT/proj_images.tbl" "$OUT/fits.tbl" "$OUT/corrections.tbl"
echo "--- 8. mBgExec ---";     mBgExec -p "$OUT/projected" "$OUT/proj_images.tbl" "$OUT/corrections.tbl" "$OUT/corrected"
echo "--- 9. mImgtbl (corrected) ---"; mImgtbl "$OUT/corrected" "$OUT/corr_images.tbl"
echo "--- 10. mAdd ---";       mAdd -p "$OUT/corrected" "$OUT/corr_images.tbl" "$HDR" "$OUT/mosaic.fits"
[ -s "$OUT/mosaic.fits" ] && echo "=== SUCCESS: mosaic.fits $(stat --printf='%s' "$OUT/mosaic.fits") bytes ==="

#!/bin/bash
set -euo pipefail

# Run the upstream Montage 10-stage mosaic pipeline. All intermediates
# (*.tbl indexes, per-tile projected/corrected/diff FITS) flow through
# $OUT — i.e., each stage's outputs land on the user's storage and the
# next stage reads them back from there. This is the producer-consumer
# pattern Montage was designed around; the storage layer underneath
# $OUT sees the full multi-stage I/O, not just a single final cp.
#
# (Earlier revisions detoured intermediates through /tmp/montage-scratch
# because wrp_cte_fuse returned ENOSYS on rename(2); Montage's atomic-
# write-then-rename then broke the chain. Backends that implement
# rename(2) correctly — NFS, dumbwarp, ext4 — don't need that detour
# and shouldn't pay its cost in lost producer-consumer traffic.)
#
# Usage: run_mosaic.sh [<raw_dir> [<region.hdr> [<out_dir> [<hosts>]]]]
RAW_DIR="${1:-/opt/montage-bench/raw_images}"
HDR="${2:-/opt/montage-bench/region.hdr}"
OUT="${3:-${HOME}/montage_out}"
HOSTS="${4:-}"

mkdir -p "$OUT"/{projected,diffs,corrected}

# If the build-time 2MASS download failed (network unreachable from the
# build container), the raw-images dir will be empty. Retry the fetch at
# runtime — the deploy container has outbound networking and IRSA may be
# reachable now. Bounded so a persistent outage fails fast with a clear
# message instead of hanging the pipeline start.
if [ ! -s "$HDR" ] || [ -z "$(ls -A "$RAW_DIR" 2>/dev/null)" ]; then
    echo "Benchmark region not pre-staged; fetching M17 J-band at runtime..."
    mkdir -p "$RAW_DIR"
    cd "$(dirname "$HDR")"
    # Montage's homegrown HTTP fetcher (svc/svc.c) can't parse
    # `user:pass@host:port` proxy URLs and dies with "Illegal port
    # number in URL". Strip $http_proxy/$https_proxy from mHdr +
    # mArchiveList (their queries usually go direct) and replace
    # mArchiveExec with a curl loop — libcurl handles authenticated
    # proxies, so the FITS pulls work behind a squid with creds.
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        mHdr "M17" 0.2 "$(basename "$HDR")"
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        mArchiveList 2mass J "M17" 0.2 0.2 remote.tbl
    fetched=0
    while read -r url; do
        fname=$(basename "$url")
        [ -z "$fname" ] && continue
        [ -f "$RAW_DIR/$fname" ] && fetched=$((fetched+1)) && continue
        if timeout 180 curl -fsSL -o "$RAW_DIR/$fname" "$url"; then
            fetched=$((fetched+1))
        else
            rm -f "$RAW_DIR/$fname"
            echo "WARN: failed to fetch $url" >&2
        fi
    done < <(grep -Ev '^[\\|]' remote.tbl | grep -oE 'https?://[^[:space:]]+')
    if [ "$fetched" -eq 0 ]; then
        echo "ERROR: cannot fetch 2MASS benchmark images (IRSA archive unreachable)" >&2
        echo "       supply raw FITS dir + region.hdr as arg 1 and 2 to /opt/run_mosaic.sh" >&2
        exit 1
    fi
    cd -
fi

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
OUT_COUNT=$(find "$OUT" -type f 2>/dev/null | wc -l)
OUT_BYTES=$(find "$OUT" -type f -printf '%s\n' 2>/dev/null | awk 'BEGIN{s=0}{s+=$1}END{print s}')
echo "=== Pipeline I/O traffic: $OUT_COUNT files, $OUT_BYTES bytes under $OUT ==="

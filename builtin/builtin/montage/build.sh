#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Montage Image Mosaic Engine — CPU-only astronomical image mosaicking.
# https://github.com/Caltech-IPAC/Montage
#
# Montage is pure C + Fortran and does not use CUDA or MPI. Individual
# binaries (e.g. mProjExec) can be parallelised by splitting input tables
# across hosts, which the multi-node compose target demonstrates.

# Plain ubuntu:24.04 is minimal — need the full C/Fortran toolchain plus
# curl + ca-certificates for the upstream git clone and 2MASS archive
# fetches. sci-hpc-base used to provide these. `file` is required by
# Montage's bundled freetype ./configure (libtool probes binary types).
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git bzip2 file \
        build-essential gfortran make \
    && rm -rf /var/lib/apt/lists/*

# Build Montage from source; upstream bundles libwcs, cfitsio, jpeg, freetype.
# Build serially — upstream's recursive Makefile has a race (subdirs run in
# parallel with -j, but lib/ outputs feed MontageLib/ and HiPS/ without
# declared dependencies, so parallel builds intermittently fail with
# "make[1]: *** [Makefile:13: all] Error 2" in the parent.
git clone --depth 1 https://github.com/Caltech-IPAC/Montage.git /opt/Montage \
    && cd /opt/Montage \
    && make

export PATH=/opt/Montage/bin:${PATH}

# Pre-stage a small 2MASS J-band benchmark region (M17) so the deploy image
# can self-test offline. mArchiveList + mArchiveExec are part of Montage.
# IPAC's IRSA archive returns empty tables intermittently under load — retry
# a few times, and if the fetch still fails, fall back to a binaries-only
# smoke test (raw_images empty) rather than failing the whole build. The
# deploy smoke test checks for raw_images contents and adapts.
mkdir -p /opt/montage-bench/raw_images
cd /opt/montage-bench
# mHdr resolves object names via SIMBAD/NED which intermittently returns
# nothing (empty header, count=0, exit 0) and later trips mProjExec with
# "Output wcsinit() failed". Pass RA/Dec directly (M17 ≈ 275.196, -16.171).
mHdr "275.196 -16.171" 0.2 region.hdr
rows=0
# IRSA's ibe search can return empty results for tight 0.2° queries under
# load. Try the tight region first, then expand to 0.4° which almost
# always returns tiles. 8 attempts with 15s spacing covers typical IRSA
# flakiness; total worst case ~2 min before falling back.
for attempt in 1 2 3 4 5 6 7 8; do
    if [ "$attempt" -le 4 ]; then
        width=0.2
    else
        width=0.4
    fi
    mArchiveList 2mass J "275.196 -16.171" "$width" "$width" remote.tbl || true
    rows=$(grep -cE '^[^|\\]' remote.tbl 2>/dev/null) || rows=0
    if [ "$rows" -gt 0 ]; then
        echo "mArchiveList returned $rows rows on attempt $attempt (width=$width)"
        break
    fi
    echo "mArchiveList returned 0 rows (attempt $attempt, width=$width); retrying..."
    sleep 15
done
if [ "$rows" -gt 0 ]; then
    # mArchiveExec occasionally hangs reading from a dead curl child after
    # all files finish downloading (upstream pipe-read bug). 10 min is
    # plenty for 6 ~1.6MB files; if exceeded, the FITS files on disk are
    # still usable and the smoke test can proceed.
    timeout 600 mArchiveExec -p raw_images remote.tbl \
        || echo "WARN: mArchiveExec timed out or failed; continuing with whatever tiles landed"
else
    echo "WARN: IRSA archive unavailable; skipping 2MASS pre-stage"
fi

# Smoke-test driver materialized inside the build container (the
# pre-refactor Dockerfile.build used COPY for run_mosaic.sh; the
# single-build-container architecture dropped host-side COPY directives,
# so we generate it here). Dockerfile.deploy then COPY --from=builder
# pulls /opt/run_mosaic.sh into the deploy image.
cat >/opt/run_mosaic.sh <<'SHEOF'
#!/bin/bash
set -e
# Minimal smoke test: build a 2MASS J-band M17 mosaic from the FITS tiles
# pre-staged at /opt/montage-bench/raw_images. Verifies the full Montage
# pipeline (mImgtbl -> mProjExec -> mAdd) when tiles were fetched at build
# time. If IRSA was unreachable at build time the directory is empty and
# we fall back to a binaries-present check.
echo "=== Montage container smoke test ==="
echo "host=$(hostname) pid=$$"
echo "mProject binary: $(ls -la /opt/Montage/bin/mProject 2>&1)"

OUT=${MONTAGE_OUT:-/tmp/montage_out}
mkdir -p "$OUT/projected"
cp /opt/montage-bench/region.hdr "$OUT/region.hdr"

if ! ls /opt/montage-bench/raw_images/*.fits >/dev/null 2>&1; then
    echo "-- raw_images empty (IRSA unreachable at build time) --"
    echo "-- binaries-only smoke test --"
    for b in mImgtbl mProjExec mAdd mProject mHdr; do
        test -x /opt/Montage/bin/$b && echo "  /opt/Montage/bin/$b present"
    done
    echo "=== Montage stack smoke test OK (binaries only) ==="
    exit 0
fi

echo "-- mImgtbl (index raw tiles) --"
/opt/Montage/bin/mImgtbl /opt/montage-bench/raw_images "$OUT/raw.tbl"

echo "-- mProjExec (reproject to common frame) --"
/opt/Montage/bin/mProjExec -p /opt/montage-bench/raw_images \
    "$OUT/raw.tbl" "$OUT/region.hdr" "$OUT/projected" "$OUT/stats.tbl"

echo "-- mImgtbl (index projections) --"
/opt/Montage/bin/mImgtbl "$OUT/projected" "$OUT/projected.tbl"

echo "-- mAdd (assemble mosaic) --"
/opt/Montage/bin/mAdd -p "$OUT/projected" "$OUT/projected.tbl" \
    "$OUT/region.hdr" "$OUT/mosaic.fits"

test -s "$OUT/mosaic.fits" || { echo "ERROR: mosaic.fits missing"; exit 1; }
ls -la "$OUT/mosaic.fits"
echo "=== Montage stack smoke test OK ==="
SHEOF

chmod +x /opt/run_mosaic.sh

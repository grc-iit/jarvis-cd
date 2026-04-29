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
#
# Split into separate statements (no `&&`): bash's `set -e` does NOT abort
# on a mid-chain failure (only the final command in a `&&` list triggers
# it), so a failed git clone used to silently continue past `cd` and
# `make` and leave the committed image with no /opt/Montage.
git clone --depth 1 https://github.com/Caltech-IPAC/Montage.git /opt/Montage
cd /opt/Montage
make
cd /

export PATH=/opt/Montage/bin:${PATH}

# Pre-stage a small 2MASS J-band benchmark region (M17) so the deploy image
# can self-test offline. mArchiveList + mArchiveExec are part of Montage.
# mArchiveExec has no internal timeout and fetches each FITS file serially
# via HTTP; a single slow IRSA/2MASS mirror will stall the container build
# indefinitely. Cap with `timeout` (10 min) and treat failure as non-fatal
# — the deploy image is still functional without the pre-staged region.
CTX=$(pwd)
mkdir -p /opt/montage-bench/raw_images
cd /opt/montage-bench
mHdr "M17" 0.2 region.hdr
mArchiveList 2mass J "M17" 0.2 0.2 remote.tbl

if ! timeout 600 mArchiveExec -p raw_images remote.tbl; then
    echo "WARN: mArchiveExec failed or timed out (10 min cap); skipping benchmark pre-staging (deploy image still usable)" >&2
fi
cd "$CTX"

# Pipeline driver — inlined as base64 by pkg._build_phase. jarvis's
# aux-file copy step suppresses docker-cp exit codes, so a silent failure
# there would surface as "cp: cannot stat run_mosaic.sh" at this point.
# Embedding the script directly removes that dependency.
printf '%s' '##RUN_MOSAIC_B64##' | base64 -d > /opt/run_mosaic.sh
chmod +x /opt/run_mosaic.sh

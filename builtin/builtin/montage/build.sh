#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Montage Image Mosaic Engine — CPU-only astronomical image mosaicking.
# https://github.com/Caltech-IPAC/Montage
#
# Montage is pure C + Fortran and does not use CUDA or MPI. Individual
# binaries (e.g. mProjExec) can be parallelised by splitting input tables
# across hosts, which the multi-node compose target demonstrates.

apt-get update && apt-get install -y --no-install-recommends \
        bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Build Montage from source; upstream bundles libwcs, cfitsio, jpeg, freetype.
git clone --depth 1 https://github.com/Caltech-IPAC/Montage.git /opt/Montage \
    && cd /opt/Montage \
    && make -j$(nproc)

export PATH=/opt/Montage/bin:${PATH}

# Pre-stage a small 2MASS J-band benchmark region (M17) so the deploy image
# can self-test offline. mArchiveList + mArchiveExec are part of Montage.
CTX=$(pwd)
mkdir -p /opt/montage-bench/raw_images \
    && cd /opt/montage-bench \
    && mHdr "M17" 0.2 region.hdr \
    && mArchiveList 2mass J "M17" 0.2 0.2 remote.tbl \
    && mArchiveExec -p raw_images remote.tbl
cd "$CTX"

# Pipeline driver (staged in CWD from pkg_dir by jarvis).
cp run_mosaic.sh /opt/run_mosaic.sh
chmod +x /opt/run_mosaic.sh

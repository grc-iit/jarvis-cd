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
#
# Both mArchiveList and mArchiveExec are best-effort: an authenticated
# http_proxy env var (e.g. squid with credentials) makes Montage's
# libwww URL parser emit "Illegal port number in URL" and exit non-zero.
# Wrap in subshell with `|| true` so the build doesn't abort under
# `set -e` when only the optional benchmark fetch is broken.
CTX=$(pwd)
mkdir -p /opt/montage-bench/raw_images
cd /opt/montage-bench
# Strip $http_proxy/$https_proxy from Montage's own tools (mHdr,
# mArchiveList, mArchiveExec) — Montage's homegrown HTTP fetcher in
# svc/svc.c can't parse `user:pass@host:port` proxy URLs and dies with
# "Illegal port number in URL" before any byte goes out. Run them with
# the proxy unset; mHdr/mArchiveList only hit IRSA metadata which is
# usually reachable direct, and we replace mArchiveExec with a curl
# loop below (curl handles authenticated proxies fine via libcurl).
(
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        mHdr "M17" 0.2 region.hdr \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        mArchiveList 2mass J "M17" 0.2 0.2 remote.tbl \
    && timeout 600 bash -c '
        # remote.tbl is an IPAC ASCII table — `\`-prefixed metadata
        # and `|`-prefixed header rows; data rows are whitespace
        # separated and contain at least one http(s) URL each.
        any=0
        while read -r url; do
            fname=$(basename "$url")
            [ -z "$fname" ] && continue
            [ -f "raw_images/$fname" ] && continue
            if curl -fsSL --max-time 180 -o "raw_images/$fname" "$url"; then
                any=1
            else
                rm -f "raw_images/$fname"
                echo "WARN: failed to fetch $url" >&2
            fi
        done < <(grep -Ev "^[\\\\|]" remote.tbl | grep -oE "https?://[^[:space:]]+")
        [ "$any" = 1 ]
    '
) || echo "WARN: 2MASS pre-staging skipped (proxy/network issue); deploy image still usable" >&2
cd "$CTX"

# Pipeline driver — inlined as base64 by pkg._build_phase. jarvis's
# aux-file copy step suppresses docker-cp exit codes, so a silent failure
# there would surface as "cp: cannot stat run_mosaic.sh" at this point.
# Embedding the script directly removes that dependency.
printf '%s' '##RUN_MOSAIC_B64##' | base64 -d > /opt/run_mosaic.sh
chmod +x /opt/run_mosaic.sh

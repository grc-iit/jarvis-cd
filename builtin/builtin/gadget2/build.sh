#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

FFTW2_VERSION=2.1.5
FFTW2_TARBALL_URL=http://www.fftw.org/fftw-${FFTW2_VERSION}.tar.gz
GADGET2_REPO=https://github.com/lukemartinlogan/gadget2.git
GADGET2_PATH=/opt/gadget2

apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        build-essential gcc cmake make pkg-config \
        openmpi-bin libopenmpi-dev \
        libgsl-dev libhdf5-dev libhdf5-mpi-dev \
    && rm -rf /var/lib/apt/lists/*

# FFTW 2.1.5 from source — Gadget2 links sfftw/sfftw_mpi/srfftw/srfftw_mpi
# (single-precision, type-prefixed, MPI variant). Modern Ubuntu only ships
# FFTW3 in the archive (ABI-incompatible with Gadget2). -fcommon works around
# GCC 10+ defaulting to -fno-common (FFTW2 has multiple non-extern globals).
curl -fsSL "${FFTW2_TARBALL_URL}" -o "/tmp/fftw-${FFTW2_VERSION}.tar.gz"
tar -xzf "/tmp/fftw-${FFTW2_VERSION}.tar.gz" -C /tmp
cd "/tmp/fftw-${FFTW2_VERSION}"
./configure --prefix=/usr/local --enable-mpi --enable-float \
    --enable-type-prefix MPICC=mpicc CC=gcc CFLAGS="-O2 -fcommon"
make -j"$(nproc)"
make install
cd /
rm -rf "/tmp/fftw-${FFTW2_VERSION}" "/tmp/fftw-${FFTW2_VERSION}.tar.gz"
ldconfig

# Gadget2 source from upstream fork; CMake build. The fork pins
# HDF5 >= 1.14.0; Ubuntu 22.04 ships 1.10.7 which exposes the same C API
# Gadget2 uses (H5Fcreate / H5Dwrite) — relax the version pin in-place.
git clone --depth 1 "${GADGET2_REPO}" "${GADGET2_PATH}"
sed -i 's/GADGET_REQUIRED_HDF5_VERSION 1.14.0/GADGET_REQUIRED_HDF5_VERSION 1.10.0/' \
    "${GADGET2_PATH}/CMakeLists.txt"
# Stock Gadget2 caps filename paths at MAXLEN_FILENAME=100. Jarvis output
# dirs under /tmp/jarvis_test_.../shared/<ppl>/<pkg>/<pkg>_out/ are ~90+
# chars; appending "timings.txt" or a snapshot suffix overflows a strcpy
# in begrun.c ("buffer overflow detected"). Bump the cap to 512.
sed -i 's/#define *MAXLEN_FILENAME *100\b/#define MAXLEN_FILENAME 512/' \
    "${GADGET2_PATH}/Gadget2/allvars.h"
cmake -S "${GADGET2_PATH}" -B "${GADGET2_PATH}/build" \
    -DBUILD_SHARED_LIBS=OFF \
    -DBUILD_MPI_TESTS=OFF -DBUILD_OpenMP_TESTS=OFF \
    -DPEANOHILBERT=ON -DWALLCLOCK=ON -DSYNCHRONIZATION=ON
cmake --build "${GADGET2_PATH}/build" --target Gadget2 -j"$(nproc)"

test -x "${GADGET2_PATH}/build/bin/Gadget2"

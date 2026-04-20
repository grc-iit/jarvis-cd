#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential python3 wget \
    openmpi-bin libopenmpi-dev
rm -rf /var/lib/apt/lists/*

# Clone WarpX (cached at this layer until URL changes)
git clone https://github.com/BLAST-WarpX/warpx.git /opt/warpx

# Build WarpX 3D CPU (AMReX NOACC backend, MPI, parallel HDF5 from
# /usr/local provided by the preceding builtin.hdf5 package).
#
# Each command on its own line so `set -e` propagates any failure
# (inside `cmd1 && cmd2` chains bash suppresses set -e for all but
# the final command — silent configure failures would slip through).
cd /opt/warpx
mkdir -p build
cd build

CC=$(which gcc) CXX=$(which g++) \
    cmake -S .. -B . \
    -DCMAKE_BUILD_TYPE=Release \
    -DWarpX_COMPUTE=NOACC \
    -DWarpX_MPI=ON \
    -DWarpX_DIMS=3 \
    -DWarpX_PRECISION=SINGLE \
    -DWarpX_PARTICLE_PRECISION=SINGLE \
    -DAMReX_HDF5=YES \
    -DCMAKE_PREFIX_PATH=/usr/local \
    -DCMAKE_CXX_FLAGS=-mcmodel=large

cmake --build . -j$(nproc)

# Sanity-check: fail loudly if the expected binary isn't produced.
test -x /opt/warpx/build/bin/warpx.3d

export PATH=/opt/warpx/build/bin:${PATH}

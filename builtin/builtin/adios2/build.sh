#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential \
    openmpi-bin libopenmpi-dev \
 && rm -rf /var/lib/apt/lists/*

# ADIOS2 ##ADIOS2_VERSION##
cd /tmp
git clone --depth 1 --branch ##ADIOS2_VERSION## https://github.com/ornladios/ADIOS2.git
cmake -S ADIOS2 -B adios2-build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DADIOS2_BUILD_EXAMPLES=OFF -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF \
    -DADIOS2_USE_MPI=##ADIOS2_USE_MPI## -DADIOS2_USE_HDF5=##ADIOS2_USE_HDF5## \
    -DADIOS2_USE_Python=OFF -DADIOS2_USE_Fortran=OFF \
    -DCMAKE_CXX_STANDARD=17
make -C adios2-build -j$(nproc)
make -C adios2-build install
ldconfig
rm -rf /tmp/ADIOS2 /tmp/adios2-build

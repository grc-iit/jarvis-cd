#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget cmake build-essential \
    openmpi-bin libopenmpi-dev \
    zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# HDF5 ##HDF5_VERSION##
cd /tmp
wget -q https://github.com/HDFGroup/hdf5/releases/download/##HDF5_VERSION##/hdf5-##HDF5_VERSION##.tar.gz
tar xzf hdf5-##HDF5_VERSION##.tar.gz
cd hdf5-##HDF5_VERSION##
CC=mpicc CXX=mpicxx cmake -B build -S . \
    -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=ON -DBUILD_STATIC_LIBS=OFF \
    -DHDF5_ENABLE_PARALLEL=##HDF5_PARALLEL## \
    -DHDF5_BUILD_CPP_LIB=OFF -DHDF5_BUILD_TOOLS=ON \
    -DHDF5_ENABLE_Z_LIB_SUPPORT=ON -DHDF5_ENABLE_SZIP_SUPPORT=OFF \
    -DHDF5_BUILD_EXAMPLES=OFF -DHDF5_BUILD_FORTRAN=OFF -DBUILD_TESTING=OFF
cmake --build build -j"${BUILD_JOBS:-4}"
cmake --install build
ldconfig
cd /tmp && rm -rf hdf5-*

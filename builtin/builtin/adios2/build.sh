#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential pkg-config \
    openmpi-bin libopenmpi-dev \
 && rm -rf /var/lib/apt/lists/*

# Fortran bindings need gfortran; Python bindings need dev headers + numpy.
if [ "##ADIOS2_USE_Fortran##" = "ON" ]; then
  apt-get update && apt-get install -y --no-install-recommends gfortran \
   && rm -rf /var/lib/apt/lists/*
fi
if [ "##ADIOS2_USE_Python##" = "ON" ]; then
  apt-get update && apt-get install -y --no-install-recommends \
      python3-dev python3-numpy python3-mpi4py \
   && rm -rf /var/lib/apt/lists/*
fi

# ADIOS2 ##ADIOS2_VERSION##
cd /tmp
rm -rf ADIOS2 adios2-build
git clone --depth 1 --branch ##ADIOS2_VERSION## https://github.com/ornladios/ADIOS2.git
cmake -S ADIOS2 -B adios2-build \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DADIOS2_BUILD_EXAMPLES=OFF -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF \
    -DADIOS2_USE_MPI=##ADIOS2_USE_MPI## \
    -DADIOS2_USE_HDF5=##ADIOS2_USE_HDF5## \
    -DADIOS2_USE_Python=##ADIOS2_USE_Python## \
    -DADIOS2_USE_Fortran=##ADIOS2_USE_Fortran## \
    -DCMAKE_CXX_STANDARD=17
make -C adios2-build -j$(nproc)
make -C adios2-build install
ldconfig
rm -rf /tmp/ADIOS2 /tmp/adios2-build

#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential python3 wget \
    openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# Clone LAMMPS develop branch (cached unless branch changes)
git clone --branch develop --depth 1 \
    https://github.com/lammps/lammps.git /opt/lammps

# Build LAMMPS with H5MD support
cd /opt/lammps \
    && mkdir -p build && cd build \
    && cmake ../cmake \
        -DCMAKE_BUILD_TYPE=Release \
        ##CMAKE_EXTRA##-DBUILD_MPI=ON \
        -DPKG_MOLECULE=ON \
        -DPKG_KSPACE=ON \
        -DPKG_RIGID=ON \
        -DPKG_H5MD=ON \
    && make -j$(nproc)

export PATH=/opt/lammps/build:${PATH}

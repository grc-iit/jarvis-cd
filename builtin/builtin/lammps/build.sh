#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential python3 wget \
    openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# Build HDF5 from source (needed for H5MD dump style)
cd /tmp \
    && wget -q https://github.com/HDFGroup/hdf5/releases/download/hdf5_1.14.6/hdf5-1.14.6.tar.gz \
    && tar xzf hdf5-1.14.6.tar.gz && cd hdf5-1.14.6 \
    && cmake -B build -S . \
        -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON -DBUILD_STATIC_LIBS=OFF \
        -DHDF5_BUILD_CPP_LIB=OFF -DHDF5_BUILD_TOOLS=OFF \
        -DHDF5_BUILD_EXAMPLES=OFF -DHDF5_BUILD_FORTRAN=OFF -DBUILD_TESTING=OFF \
    && cmake --build build -j$(nproc) && cmake --install build \
    && ldconfig && cd /tmp && rm -rf hdf5-*

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

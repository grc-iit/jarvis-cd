#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# OpenFOAM-dev — open-source CFD framework.
# Builds OpenFOAM-dev + ThirdParty-dev from source against system MPI.
# ADIOS2 (v##ADIOS2_VERSION##) is installed alongside for custom function
# objects, coupled workflows, and in-situ analysis; OpenFOAM-dev has no
# native ADIOS2 integration.
#
# Base image is expected to provide: build-essential, cmake, git, openmpi,
# hdf5, and ssh. This script is run inside a jarvis build container derived
# from ##BASE_IMAGE##.

# ---- OpenFOAM build dependencies not already in the base image -----------------
apt-get update && apt-get install -y --no-install-recommends \
        flex libfl-dev zlib1g-dev libxt-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- ADIOS2 ##ADIOS2_VERSION## (MPI + HDF5) -------------------------------------
git clone --branch v##ADIOS2_VERSION## --depth 1 \
        https://github.com/ornladios/ADIOS2.git /tmp/adios2-src \
    && cmake -S /tmp/adios2-src -B /tmp/adios2-build \
        -DCMAKE_INSTALL_PREFIX=/opt/adios2 \
        -DCMAKE_BUILD_TYPE=Release \
        -DADIOS2_USE_MPI=ON \
        -DADIOS2_USE_HDF5=ON \
        -DHDF5_ROOT=/opt/hdf5 \
        -DADIOS2_USE_Fortran=OFF \
        -DADIOS2_USE_Python=OFF \
        -DADIOS2_USE_ZeroMQ=OFF \
        -DADIOS2_BUILD_EXAMPLES=OFF \
        -DBUILD_TESTING=OFF \
    && cmake --build /tmp/adios2-build -j$(nproc) \
    && cmake --install /tmp/adios2-build \
    && rm -rf /tmp/adios2-src /tmp/adios2-build

export ADIOS2_ROOT=/opt/adios2
export PATH=/opt/adios2/bin:${PATH}
export LD_LIBRARY_PATH=/opt/adios2/lib:${LD_LIBRARY_PATH}

# ---- OpenFOAM-dev + ThirdParty-dev ----------------------------------------------
# Both repos must sit side-by-side under the same parent directory.
mkdir -p /opt/OpenFOAM \
    && git clone --depth 1 https://github.com/OpenFOAM/OpenFOAM-dev.git \
        /opt/OpenFOAM/OpenFOAM-dev \
    && git clone --depth 1 https://github.com/OpenFOAM/ThirdParty-dev.git \
        /opt/OpenFOAM/ThirdParty-dev

# Build ThirdParty components (Scotch, etc.) then OpenFOAM itself.
# WM_MPLIB defaults to SYSTEMOPENMPI which picks up the base image's OpenMPI.
/bin/bash -c '\
    source /opt/OpenFOAM/OpenFOAM-dev/etc/bashrc \
    && cd /opt/OpenFOAM/ThirdParty-dev && ./Allwmake \
    && cd /opt/OpenFOAM/OpenFOAM-dev   && ./Allwmake -j'

# Source the OpenFOAM environment for interactive shells
echo "source /opt/OpenFOAM/OpenFOAM-dev/etc/bashrc" >> /root/.bashrc

export FOAM_INST_DIR=/opt/OpenFOAM

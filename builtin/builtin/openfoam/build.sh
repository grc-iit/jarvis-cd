#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# OpenFOAM-dev — open-source CFD framework.
# Builds OpenFOAM-dev + ThirdParty-dev from source against system MPI.
# ADIOS2 (and HDF5) are supplied by their respective jarvis Library
# packages and injected under /usr/local before this script runs — add
# builtin.adios2 (and builtin.hdf5 if you need it) *before*
# builtin.openfoam in the pipeline YAML.

# ---- OpenFOAM build dependencies not already in the base image -----------------
apt-get update && apt-get install -y --no-install-recommends \
        flex libfl-dev zlib1g-dev libxt-dev \
    && rm -rf /var/lib/apt/lists/*

# ADIOS2 is already at /usr/local from the adios2 Library; expose for any
# OpenFOAM add-ons that probe the env.
export ADIOS2_ROOT=/usr/local

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

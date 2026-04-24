#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# PyFLEXTRKR — Python FLEXible object TRacKeR for atmospheric feature
# tracking (mesoscale convective systems, convective cells, etc.).
# CPU-only. Mirrors awesome-scienctific-applications/pyflextrkr/Dockerfile.

# Extra runtime deps for netCDF4, scikit-image wheels, and mpi4py build.
# openmpi headers come from the base image (libopenmpi-dev).
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        python3 python3-venv python3-dev build-essential pkg-config \
        openmpi-bin libopenmpi-dev \
        libhdf5-dev libnetcdf-dev libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

# Dedicated venv (no conda) mirroring the bare-metal Ares recipe.
python3 -m venv /opt/pyflextrkr-env \
    && /opt/pyflextrkr-env/bin/pip install --upgrade pip wheel

# Clone PyFLEXTRKR pinned to a known-working tag and editable-install.
git clone --depth 1 https://github.com/FlexTRKR/PyFLEXTRKR.git /opt/PyFLEXTRKR \
    && /opt/pyflextrkr-env/bin/pip install --no-cache-dir -e /opt/PyFLEXTRKR

# Multi-node parallelism stack:
#   - dask-mpi + mpi4py: enables run_parallel=2 under mpirun
#   - h5netcdf + h5py:  avoids the netCDF4 LRU-cache deadlock seen on
#     shared filesystems when many Dask workers open cloudid files
#   - healpy: required by pyflextrkr.remap_healpix_zarr (upstream added
#     this as an unconditional import).
/opt/pyflextrkr-env/bin/pip install --no-cache-dir \
        mpi4py dask-mpi h5netcdf h5py healpy

# Pipeline drivers — staged in CWD from pkg_dir by jarvis
# (equivalent to the apps-repo Dockerfile's COPY directives).
cp run_demo.sh            /opt/run_demo.sh
cp run_demo_multinode.sh  /opt/run_demo_multinode.sh
cp run_mcs_tbpf_mpi.py    /opt/run_mcs_tbpf_mpi.py
chmod +x /opt/run_demo.sh /opt/run_demo_multinode.sh

export PATH=/opt/pyflextrkr-env/bin:${PATH}
export PYTHONNOUSERSITE=1

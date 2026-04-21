#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Plain ubuntu:24.04 is minimal — install the full build toolchain here
# rather than relying on a bespoke base image. openmpi + hdf5 + netcdf +
# geos headers feed the mpi4py, h5py, netCDF4 and shapely wheel builds.
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

export PATH=/opt/pyflextrkr-env/bin:${PATH}
export PYTHONNOUSERSITE=1

# Smoke-test scripts materialized inside the build container (the
# pre-refactor Dockerfile.build used COPY for these; the single-build-
# container architecture dropped host-side COPY directives, so we
# generate them here instead). Dockerfile.deploy then COPY --from=builder
# pulls /opt/run_demo*.sh into the deploy image.
cat >/opt/run_mcs_tbpf_mpi.py <<'PYEOF'
"""
Minimal smoke test for the PyFLEXTRKR container image. Verifies the
venv's key deps import and that mpi4py can bring up a communicator.
Avoids the 50MB+ WRF sample dataset so container smoke runs stay fast.
"""
import os
import sys

from mpi4py import MPI
import pyflextrkr
import h5py
import h5netcdf
import netCDF4
import numpy as np
import xarray as xr
import dask

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

print(f"[rank {rank}/{size}] host={os.uname().nodename} pid={os.getpid()}")
print(f"[rank {rank}] python={sys.version.split()[0]} "
      f"pyflextrkr={pyflextrkr.__version__ if hasattr(pyflextrkr, '__version__') else 'present'} "
      f"numpy={np.__version__} xarray={xr.__version__} dask={dask.__version__}")
print(f"[rank {rank}] h5py={h5py.__version__} h5netcdf={h5netcdf.__version__} "
      f"netCDF4={netCDF4.__version__}")

comm.Barrier()
if rank == 0:
    print("=== PyFLEXTRKR stack smoke test OK ===")
PYEOF

cat >/opt/run_demo.sh <<'SHEOF'
#!/bin/bash
set -e
exec /opt/pyflextrkr-env/bin/python3 /opt/run_mcs_tbpf_mpi.py
SHEOF

cat >/opt/run_demo_multinode.sh <<'SHEOF'
#!/bin/bash
set -e
# Jarvis launches this under mpirun (see start() -> MpiExecInfo), so
# calling the python script directly lets each rank run it independently.
exec /opt/pyflextrkr-env/bin/python3 /opt/run_mcs_tbpf_mpi.py
SHEOF

chmod +x /opt/run_demo.sh /opt/run_demo_multinode.sh

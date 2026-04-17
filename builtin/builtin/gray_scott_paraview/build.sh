#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# ParaView — Parallel visualization for Gray-Scott reaction-diffusion analysis
# Builds ParaView with: +qt +adios2 +fides +mpi +libcatalyst +python
#
# ADIOS2 provides the data transport layer (SST/BP) from the Gray-Scott
# simulation; Fides reads ADIOS2 data into ParaView's VTK pipeline; Catalyst
# enables in-situ analysis without writing intermediate files.

ADIOS2_VERSION="${ADIOS2_VERSION:-v2.10.2}"
CATALYST_VERSION="${CATALYST_VERSION:-v2.0.0}"
PARAVIEW_VERSION="${PARAVIEW_VERSION:-v5.13.1}"

# ---- System packages: Qt6, Python dev, OpenGL/EGL, build tools -----------------
apt-get update && apt-get install -y --no-install-recommends \
        python3-dev python3-numpy python3-mpi4py \
        ninja-build pkg-config \
        qt6-base-dev qt6-tools-dev qt6-5compat-dev \
        libqt6svg6-dev libqt6opengl6-dev \
        libxkbcommon-dev \
        libopengl-dev libgl1-mesa-dev libegl1-mesa-dev libglu1-mesa-dev \
        libxt-dev libxext-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- ADIOS2 v2.10.2 (MPI + HDF5 + Python) --------------------------------------
# H5_USE_114_API: base image ships HDF5 2.0.0; ADIOS2 targets the 1.14 API.
# Default file engine is BP5 (since ADIOS2 v2.9); BP4, SST, and other engines
# remain available.
git clone --branch ${ADIOS2_VERSION} --depth 1 \
        https://github.com/ornladios/ADIOS2.git /tmp/adios2-src \
    && cmake -S /tmp/adios2-src -B /tmp/adios2-build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/adios2 \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_FLAGS="-DH5_USE_114_API" \
        -DCMAKE_CXX_FLAGS="-DH5_USE_114_API" \
        -DADIOS2_USE_MPI=ON \
        -DADIOS2_USE_HDF5=ON \
        -DHDF5_ROOT=/opt/hdf5 \
        -DADIOS2_USE_Python=ON \
        -DADIOS2_USE_Fortran=OFF \
        -DADIOS2_BUILD_EXAMPLES=OFF \
        -DBUILD_TESTING=OFF \
    && cmake --build /tmp/adios2-build -j$(nproc) \
    && cmake --install /tmp/adios2-build \
    && rm -rf /tmp/adios2-src /tmp/adios2-build

export ADIOS2_DIR=/opt/adios2
export PATH=/opt/adios2/bin:${PATH}
export LD_LIBRARY_PATH=/opt/adios2/lib:${LD_LIBRARY_PATH}

# ---- Catalyst v2.0.0 (in-situ analysis API) ------------------------------------
# Standalone Catalyst library that simulations link against.  ParaView provides
# the Catalyst *implementation* at runtime.
git clone --branch ${CATALYST_VERSION} --depth 1 \
        https://gitlab.kitware.com/paraview/catalyst.git /tmp/catalyst-src \
    && cmake -S /tmp/catalyst-src -B /tmp/catalyst-build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/catalyst \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_BUILD_TYPE=Release \
        -DCATALYST_BUILD_TESTING=OFF \
    && cmake --build /tmp/catalyst-build -j$(nproc) \
    && cmake --install /tmp/catalyst-build \
    && rm -rf /tmp/catalyst-src /tmp/catalyst-build

export catalyst_DIR=/opt/catalyst
export LD_LIBRARY_PATH=/opt/catalyst/lib:${LD_LIBRARY_PATH}

# ---- ParaView v5.13.1 ----------------------------------------------------------
# +qt:          Qt6 GUI (paraview client)
# +mpi:         Parallel rendering and data server (pvserver / pvbatch)
# +python:      Python scripting and pvpython / pvbatch
# +adios2:      VTK IOADIOS2 reader for .bp files and SST streams
# +fides:       Fides reader (ADIOS2 + VTK-m based schema-driven reader)
# +libcatalyst: In-situ Catalyst implementation
git clone --branch ${PARAVIEW_VERSION} --depth 1 \
        --recursive --shallow-submodules \
        https://gitlab.kitware.com/paraview/paraview.git /tmp/paraview-src \
    && cmake -S /tmp/paraview-src -B /tmp/paraview-build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/paraview \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_BUILD_TYPE=Release \
        -DPARAVIEW_USE_QT=ON \
        -DPARAVIEW_USE_MPI=ON \
        -DPARAVIEW_USE_PYTHON=ON \
        -DVTK_MODULE_ENABLE_VTK_IOADIOS2=YES \
        -DPARAVIEW_ENABLE_FIDES=ON \
        -DPARAVIEW_ENABLE_CATALYST=ON \
        -DADIOS2_DIR=/opt/adios2 \
        -Dcatalyst_DIR=/opt/catalyst \
        -DPARAVIEW_BUILD_TESTING=OFF \
        -DPARAVIEW_BUILD_EXAMPLES=OFF \
        -DVTK_SMP_IMPLEMENTATION_TYPE=OpenMP \
    && cmake --build /tmp/paraview-build -j$(nproc) \
    && cmake --install /tmp/paraview-build \
    && rm -rf /tmp/paraview-src /tmp/paraview-build

export PATH=/opt/paraview/bin:${PATH}
export LD_LIBRARY_PATH=/opt/paraview/lib:${LD_LIBRARY_PATH}
export PYTHONPATH=/opt/paraview/lib/python3.12/site-packages:/opt/adios2/local/lib/python3.12/dist-packages:${PYTHONPATH}

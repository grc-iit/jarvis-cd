#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# WRF — Weather Research and Forecasting Model
# Builds WRF v4.6.0 with NetCDF support. HDF5 and ADIOS2 provided by Library packages.
# CPU-only (MPI parallelism); CUDA layer is inherited from base but unused by WRF.

# ---- Fortran compiler and WRF build dependencies --------------------------------
apt-get update && apt-get install -y --no-install-recommends \
        gfortran libpng-dev zlib1g-dev libaec-dev m4 csh file perl \
    && rm -rf /var/lib/apt/lists/*

# ---- JasPer 4.2.4 (GRIB2 support for WPS) --------------------------------------
curl -L -o /tmp/jasper.tar.gz \
        "https://github.com/jasper-software/jasper/releases/download/version-4.2.4/jasper-4.2.4.tar.gz" \
    && cd /tmp && tar xzf jasper.tar.gz \
    && cmake -S jasper-4.2.4 -B jasper-build \
        -DCMAKE_INSTALL_PREFIX=/opt/jasper \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_BUILD_TYPE=Release \
        -DJAS_ENABLE_DOC=OFF \
        -DJAS_ENABLE_PROGRAMS=OFF \
    && cmake --build jasper-build -j$(nproc) \
    && cmake --install jasper-build \
    && rm -rf /tmp/jasper*

# ---- NetCDF-C 4.9.2 -------------------------------------------------------------
# H5_USE_114_API: base image ships HDF5 2.0.0; NetCDF 4.9.2 targets the 1.14 API.
curl -L -o /tmp/netcdf-c.tar.gz \
        "https://github.com/Unidata/netcdf-c/archive/refs/tags/v4.9.2.tar.gz" \
    && cd /tmp && tar xzf netcdf-c.tar.gz \
    && cmake -S netcdf-c-4.9.2 -B netcdf-c-build \
        -DCMAKE_INSTALL_PREFIX=/opt/netcdf \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_FLAGS="-DH5_USE_114_API" \
        -DHDF5_ROOT=/usr/local \
        -DHDF5_INCLUDE_DIR=/usr/local/include \
        -DHAVE_HDF5_ZLIB=ON \
        -DUSE_HDF5_SZIP=ON \
        -DENABLE_FILTER_SZIP=ON \
        -DENABLE_DAP=OFF \
        -DENABLE_BYTERANGE=OFF \
        -DENABLE_TESTS=OFF \
    && cmake --build netcdf-c-build -j$(nproc) \
    && cmake --install netcdf-c-build \
    && sed -i 's|hdf5_hl-shared|/usr/local/lib/libhdf5_hl.so|g; s|hdf5-shared|/usr/local/lib/libhdf5.so|g' \
       /opt/netcdf/lib/cmake/netCDF/netCDFTargets*.cmake \
    && rm -rf /tmp/netcdf-c*

# ---- NetCDF-Fortran 4.6.1 -------------------------------------------------------
curl -L -o /tmp/netcdf-fortran.tar.gz \
        "https://github.com/Unidata/netcdf-fortran/archive/refs/tags/v4.6.1.tar.gz" \
    && cd /tmp && tar xzf netcdf-fortran.tar.gz \
    && cmake -S netcdf-fortran-4.6.1 -B netcdf-f-build \
        -DCMAKE_INSTALL_PREFIX=/opt/netcdf \
        -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_BUILD_TYPE=Release \
        -DnetCDF_ROOT=/opt/netcdf \
        -DHAVE_DEF_VAR_SZIP=ON \
    && cmake --build netcdf-f-build -j$(nproc) \
    && cmake --install netcdf-f-build \
    && rm -rf /tmp/netcdf-fortran*

export NETCDF=/opt/netcdf
export PATH=/opt/netcdf/bin:${PATH}
export LD_LIBRARY_PATH=/opt/netcdf/lib:${LD_LIBRARY_PATH}

# ---- WRF v4.6.0 -----------------------------------------------------------------
export WRFIO_NCD_LARGE_FILE_SUPPORT=1
export JASPERLIB=/opt/jasper/lib
export JASPERINC=/opt/jasper/include
export HDF5=/usr/local

git clone --branch v4.6.0 --depth 1 \
        https://github.com/wrf-model/WRF.git /opt/WRF

# Configure WRF
#   Option 34 = GNU (gfortran/gcc) dmpar (MPI) on x86_64 Linux
#   Nesting  1 = basic
# If your platform differs, run ./configure interactively to find the right number.
cd /opt/WRF \
    && printf '34\n1\n' | ./configure

# Compile real-data target (wrf.exe, real.exe, ndown.exe, tc.exe)
# WRF compile may return non-zero even on success; verify via ls.
cd /opt/WRF \
    && ./compile em_real -j $(nproc) 2>&1 | tee /tmp/wrf_compile.log \
    ; ls main/wrf.exe main/real.exe

# Also compile an ideal case so docker-compose can validate without external data.
cd /opt/WRF \
    && ./compile em_quarter_ss -j $(nproc) 2>&1 | tee /tmp/wrf_compile_ideal.log \
    ; ls main/ideal.exe

export WRF_DIR=/opt/WRF
export PATH=/opt/WRF/main:${PATH}

# ---- WPS v4.6.0 (WRF Preprocessing System) --------------------------------------
# NOTE: ungrib.exe requires JasPer 2.x (jpc_decode API); JasPer 4.x removed it.
# geogrid.exe and metgrid.exe build successfully.
git clone --branch v4.6.0 --depth 1 \
        https://github.com/wrf-model/WPS.git /opt/WPS

# WPS configure: option 3 = Linux x86_64 gfortran dmpar (with GRIB2)
cd /opt/WPS \
    && printf '3\n' | ./configure \
    && sed -i 's/-lnetcdf/-lnetcdff -lnetcdf/g' configure.wps \
    && ./compile 2>&1 | tee /tmp/wps_compile.log \
    ; ls geogrid.exe metgrid.exe

export PATH=/opt/WPS:${PATH}

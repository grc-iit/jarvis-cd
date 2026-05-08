#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

CM1_VERSION=cm1r20.3
CM1_TARBALL_URL=https://www2.mmm.ucar.edu/people/bryan/cm1/${CM1_VERSION}.tar.gz

# CM1 build deps: gfortran + OpenMPI + NetCDF (for file_format=netcdf).
apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        build-essential gfortran make \
        libopenmpi-dev openmpi-bin \
        libnetcdf-dev libnetcdff-dev \
    && rm -rf /var/lib/apt/lists/*

curl -fsSL "${CM1_TARBALL_URL}" -o "/tmp/${CM1_VERSION}.tar.gz"
tar -xzf "/tmp/${CM1_VERSION}.tar.gz" -C /opt
ln -sfn "/opt/${CM1_VERSION}" /opt/cm1
rm "/tmp/${CM1_VERSION}.tar.gz"

# CM1's stock Makefile ships with every hardware/compiler block commented out.
# Prepend a GNU+MPI+netCDF block so it wins over later blocks. The
# free-line-length flag is required: module_mp_nssl_2mom.F has lines >132
# chars that gfortran would otherwise reject with -Werror=line-truncation.
cd /opt/cm1/src
printf '%s\n' \
    'FC = mpif90' \
    'CPP = cpp -C -P -traditional -Wno-invalid-pp-token -ffreestanding' \
    'DM = -DMPI' \
    'OUTPUTINC = -I/usr/include' \
    'OUTPUTLIB =' \
    'OUTPUTOPT = -DNETCDF -DNCFPLUS' \
    'LINKOPTS = -lnetcdff -lnetcdf' \
    'OPTS = -ffree-form -ffree-line-length-none -ffixed-line-length-none -O2 -finline-functions -fallow-argument-mismatch' \
    '' > /tmp/cm1_config
cat /tmp/cm1_config Makefile > Makefile.new
mv Makefile.new Makefile
rm /tmp/cm1_config
make -j$(nproc)

test -x /opt/cm1/run/cm1.exe

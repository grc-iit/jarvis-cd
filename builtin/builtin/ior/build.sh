#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Build dependencies (IOR + Darshan)
apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    build-essential autoconf automake libtool \
    zlib1g-dev \
    openmpi-bin libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*

# Download IOR release tarball (includes pre-generated configure)
mkdir -p /opt/ior && curl -sL https://github.com/hpc/ior/releases/download/3.3.0/ior-3.3.0.tar.gz \
    | tar xz --strip-components=1 -C /opt/ior

# Configure and build IOR
cd /opt/ior \
    && ./configure --prefix=/opt/ior/install \
    && make -j$(nproc) \
    && make install

# Download and build Darshan runtime with MPI support
mkdir -p /opt/darshan && curl -sL https://github.com/darshan-hpc/darshan/archive/refs/tags/darshan-3.4.4.tar.gz \
    | tar xz --strip-components=1 -C /opt/darshan

cd /opt/darshan/darshan-runtime \
    && autoreconf -ivf \
    && ./configure --prefix=/opt/darshan/install \
        --with-log-path-by-env=DARSHAN_LOG_DIR \
        --with-jobid-env=PBS_JOBID \
        CC=mpicc \
    && make -j$(nproc) install

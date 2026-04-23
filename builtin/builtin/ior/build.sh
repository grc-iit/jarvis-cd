#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Build dependencies (IOR + Darshan)
apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    build-essential autoconf automake libtool \
    zlib1g-dev \
    openmpi-bin libopenmpi-dev \
    openssh-server openssh-client \
    && rm -rf /var/lib/apt/lists/*

# SSH setup for MPI multi-container (host keys + root key, picked up by Dockerfile.deploy)
mkdir -p /var/run/sshd /root/.ssh \
    && ssh-keygen -A \
    && ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 \
    && cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys \
    && chmod 700 /root/.ssh \
    && chmod 600 /root/.ssh/authorized_keys \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\n" >> /etc/ssh/ssh_config

# Download IOR release tarball (includes pre-generated configure)
mkdir -p /opt/ior && curl -sL https://github.com/hpc/ior/releases/download/3.3.0/ior-3.3.0.tar.gz \
    | tar xz --strip-components=1 -C /opt/ior

# Patch IOR 3.3.0 HDF5 version check so it compiles with HDF5 2.x
# The original condition (H5_VERS_MAJOR > 0 && H5_VERS_MINOR > 5) fails for
# HDF5 2.1.x because minor=1 is not >5; fix to handle major>=2 correctly.
sed -i 's/#if (H5_VERS_MAJOR > 0 && H5_VERS_MINOR > 5)/#if (H5_VERS_MAJOR > 1 || H5_VERS_MINOR > 5)/' \
    /opt/ior/src/aiori-HDF5.c

# Configure and build IOR (with HDF5 if available)
HDF5_FLAG=""
if [ -f /usr/local/include/hdf5.h ]; then
    HDF5_FLAG="--with-hdf5"
fi
cd /opt/ior \
    && ./configure --prefix=/opt/ior/install $HDF5_FLAG \
        LDFLAGS="-L/usr/local/lib" CPPFLAGS="-I/usr/local/include" \
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

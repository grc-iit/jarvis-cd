#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential python3 wget \
    openmpi-bin libopenmpi-dev \
    openssh-server openssh-client \
    && rm -rf /var/lib/apt/lists/*

# SSH setup for MPI over apptainer instance (mirrors builtin/ior/build.sh)
mkdir -p /var/run/sshd /root/.ssh \
    && ssh-keygen -A \
    && ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 \
    && cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys \
    && chmod 700 /root/.ssh \
    && chmod 600 /root/.ssh/authorized_keys \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\n" >> /etc/ssh/ssh_config

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
    && make -j"${BUILD_JOBS:-4}"

ln -sf /opt/lammps/build/lmp /usr/local/bin/lmp

export PATH=/opt/lammps/build:${PATH}

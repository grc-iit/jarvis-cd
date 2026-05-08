#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential \
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

# Clone VPIC-Kokkos with bundled Kokkos (cached until repo changes)
git clone --recursive https://github.com/lanl/vpic-kokkos.git /opt/vpic-kokkos

# Build VPIC core library
cd /opt/vpic-kokkos \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        ##CMAKE_FLAGS## \
    && cmake --build build -j"${BUILD_JOBS:-4}"

##POST_BUILD##
# Drop CUDA compat libs (apptainer --nv bind-mounts the host libcuda.so.1;
# in-image compat libs can shadow it and cause runtime errors)
rm -rf /usr/local/cuda/compat

# Runtime LD_LIBRARY_PATH so VPIC's deck binary (harris.Linux) finds
# libcuda.so.1 — apptainer --nv puts host libs at /.singularity.d/libs.
# Use /etc/environment (PAM sources it for all sshd sessions, not just
# login shells — profile.d doesn't fire for non-interactive ssh).
echo 'LD_LIBRARY_PATH="/.singularity.d/libs:/usr/local/cuda/lib64:/usr/local/cuda/lib64/stubs"' >> /etc/environment

export PATH=/opt/vpic-kokkos/build/bin:${PATH}

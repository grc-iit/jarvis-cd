#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential gfortran \
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

# Clone Nyx with AMReX submodule (cached until repo changes)
git clone --recursive https://github.com/AMReX-Astro/Nyx.git /opt/Nyx \
    && cd /opt/Nyx/subprojects/amrex && git checkout development

# Build Nyx HydroTests
cd /opt/Nyx \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DNyx_MPI=YES \
        -DNyx_OMP=NO \
        -DNyx_HYDRO=YES \
        -DNyx_HEATCOOL=NO \
        ##HDF5_FLAGS####GPU_FLAGS##-DAMReX_PRECISION=SINGLE \
        -DAMReX_PARTICLES_PRECISION=SINGLE \
        -DCMAKE_C_COMPILER="$(which gcc)" \
        -DCMAKE_CXX_COMPILER="$(which g++)" \
    && cmake --build build --target nyx_HydroTests -j"${BUILD_JOBS:-4}"

ln -sf /opt/Nyx/build/Exec/HydroTests/nyx_HydroTests /usr/bin/nyx_HydroTests

# Drop CUDA compat libs — apptainer --nv bind-mounts the host's libcuda.so.1
# (newer driver); compat libs in the image can win resolution and cause
# cudaErrorInsufficientDriver (error 35) at AMReX's GpuDevice init.
rm -rf /usr/local/cuda/compat

# Runtime LD_LIBRARY_PATH so AMReX's CUDA runtime loads the host libcuda.so.1
# apptainer --nv bind-mounts into /.singularity.d/libs, not the in-image stub
# at /usr/local/cuda/lib64/stubs. Apply via sshd SetEnv — our pipeline starts
# sshd with UsePAM=no, so /etc/environment is not sourced for mpirun sessions.
# SetEnv runs unconditionally on every ssh connection (OpenSSH 7.8+).
echo 'SetEnv LD_LIBRARY_PATH=/.singularity.d/libs:/usr/local/cuda/lib64' >> /etc/ssh/sshd_config

export PATH=/opt/Nyx/build/Exec/HydroTests:${PATH}

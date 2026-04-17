#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

# Xcompact3d — High-order finite-difference flow solver (DNS/LES)
# Builds 2DECOMP&FFT and Incompact3d. CPU-only (MPI parallelism).
# ADIOS2 is provided by the adios2 Library package (installed to /opt/adios2).

# ---- System packages ------------------------------------------------------------
apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake curl wget git ca-certificates \
        gfortran \
        openmpi-bin libopenmpi-dev \
        python3 python3-numpy \
        openssh-server openssh-client \
    && rm -rf /var/lib/apt/lists/*

# ---- SSH setup for MPI multi-node (simulation only) -----------------------------
mkdir -p /var/run/sshd /root/.ssh \
    && ssh-keygen -A \
    && ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 \
    && cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys \
    && chmod 700 /root/.ssh \
    && chmod 600 /root/.ssh/authorized_keys \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\n" >> /etc/ssh/ssh_config

# ---- 2DECOMP&FFT v2.0.4 (with ADIOS2 IO backend) -------------------------------
# Must be pre-built; the Incompact3d auto-download does NOT pass IO_BACKEND through.
# Patch: add BP5 engine support (backported from v2.1.0 commit 043759c).
curl -L -o /tmp/2decomp.tar.gz \
        "https://github.com/2decomp-fft/2decomp-fft/archive/refs/tags/v2.0.4.tar.gz" \
    && cd /tmp && tar -xzf 2decomp.tar.gz 2>/dev/null ; test -d /tmp/2decomp-fft-2.0.4 \
    && mv /tmp/2decomp-fft-2.0.4 /opt/2decomp-fft \
    && rm /tmp/2decomp.tar.gz

# Backport BP5 engine support from v2.1.0 (commit 043759c) into v2.0.4,
# and replace the fatal error on unknown engines with a safe fallback.
sed -i 's/ext = ".bp4"/ext = ".bp4"\n      else if (io%engine_type == "BP5") then\n         ext = ".bp5"/' \
        /opt/2decomp-fft/src/io.f90 \
    && sed -i '/Unkown engine type/,/stop/c\         ext = ""' \
        /opt/2decomp-fft/src/io.f90

cd /opt/2decomp-fft \
    && FC=mpif90 cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DIO_BACKEND=adios2 \
        -Dadios2_DIR=/opt/adios2/lib/cmake/adios2 \
    && cmake --build build -j$(nproc) \
    && cmake --install build

# ---- Incompact3d (Xcompact3d solver) --------------------------------------------
curl -L -o /tmp/incompact3d.tar.gz \
        "https://github.com/xcompact3d/Incompact3d/archive/refs/tags/v5.0.tar.gz" \
    && cd /tmp && tar -xzf incompact3d.tar.gz 2>/dev/null ; test -d /tmp/Incompact3d-5.0 \
    && mv /tmp/Incompact3d-5.0 /opt/Incompact3d \
    && rm /tmp/incompact3d.tar.gz

cd /opt/Incompact3d \
    && FC=mpif90 cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DIO_BACKEND=adios2 \
        -Dadios2_DIR=/opt/adios2/lib/cmake/adios2 \
        -Ddecomp2d_DIR=/opt/2decomp-fft/build/opt/lib/decomp2d \
        -DBUILD_TESTING=ON \
    && cmake --build build -j$(nproc) \
    && cmake --install build

# Custom ADIOS2 config using BP5 engine for all Xcompact3d IO
# NOTE: adios2_config.xml must be copied separately (was a COPY directive)

export PATH=/opt/Incompact3d/build/opt/bin:${PATH}

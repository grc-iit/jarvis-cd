#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential wget \
    openmpi-bin libopenmpi-dev \
    openssh-server openssh-client \
    python3 python3-dev \
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

CUDA_ARCH="${CUDA_ARCH:-##CUDA_ARCH##}"

# Clone WarpX (cached at this layer until URL changes)
git clone https://github.com/BLAST-WarpX/warpx.git /opt/warpx

# Build WarpX 3D CUDA+MPI+HDF5
# Ordered for maximum layer cache reuse: cmake configure then make
cd /opt/warpx \
    && mkdir -p build && cd build \
    && CC=$(which gcc) CXX=$(which g++) CUDACXX=$(which nvcc) CUDAHOSTCXX=$(which g++) \
       cmake -S .. -B . \
        -DCMAKE_BUILD_TYPE=Release \
        -DWarpX_COMPUTE=CUDA \
        -DWarpX_MPI=ON \
        -DWarpX_DIMS=3 \
        -DWarpX_PRECISION=SINGLE \
        -DWarpX_PARTICLE_PRECISION=SINGLE \
        -DAMReX_HDF5=YES \
        -DopenPMD_USE_HDF5=ON \
        "-DCMAKE_PREFIX_PATH=/usr/local" \
        "-DAMReX_CUDA_ARCH=${CUDA_ARCH}" \
        "-DCMAKE_CXX_FLAGS=-mcmodel=large" \
        "-DCMAKE_CUDA_FLAGS=-Xcompiler -mcmodel=large --diag-suppress=222 --diag-suppress=221" \
    && cmake --build . -j"${BUILD_JOBS:-4}"

# Symlink the warpx binary under canonical names regardless of feature suffix.
# Actual binary has names like warpx.3d.MPI.CUDA.SP.PSP.OPMD.EB.QED depending
# on build flags — find the one that exists and symlink it.
built_bin=$(ls /opt/warpx/build/bin/warpx.3d* 2>/dev/null | head -1)
if [ -n "$built_bin" ]; then
    ln -sf "$built_bin" /usr/local/bin/warpx.3d
    ln -sf "$built_bin" /opt/warpx/build/bin/warpx.3d
fi

# Drop CUDA compat libs — apptainer --nv bind-mounts the host's libcuda.so.1;
# in-image compat libs can shadow and cause cudaErrorInsufficientDriver (35)
# at AMReX's GpuDevice init.
rm -rf /usr/local/cuda/compat

# Runtime LD_LIBRARY_PATH so any CUDA-linked binary finds libcuda.so.1
# (apptainer --nv puts host libs at /.singularity.d/libs).
cat > /etc/profile.d/cuda-runtime.sh <<'EOF'
export LD_LIBRARY_PATH=/.singularity.d/libs:/usr/local/cuda/lib64:/usr/local/cuda/lib64/stubs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
EOF
chmod +x /etc/profile.d/cuda-runtime.sh

export PATH=/opt/warpx/build/bin:${PATH}

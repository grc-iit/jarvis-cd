#!/bin/bash
# Build LAMMPS with Kokkos SYCL backend (JIT mode) for Intel PVC.
# Base image: intel/oneapi-hpckit (provides icpx, Level Zero loader, impi).
set -e

export DEBIAN_FRONTEND=noninteractive

# Base build tooling + OpenMPI + SSH server for multi-node MPI-over-SSH.
#
# Intel GPU runtime (intel-opencl-icd, libze-intel-gpu1, libze1) is
# REQUIRED because intel/oneapi-hpckit ships the SYCL compiler (icpx) and
# Level Zero *loader* (libze_loader.so) but NOT the Level Zero GPU
# *backend* (libze_intel_gpu.so). Without the backend, zeDriverGet()
# returns zero devices and SYCL reports "no GPU available" at runtime.
apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential python3 gnupg wget \
    openmpi-bin libopenmpi-dev \
    openssh-server openssh-client

# Intel GPU user-mode runtime (Level Zero + OpenCL) from Intel's APT repo.
# The hpckit base image configures this repo with a different keyring
# path; if we add our own entry with a different Signed-By, apt-get update
# errors with "Conflicting values for Signed-By" and silently skips
# subsequent installs. Purge pre-existing entries first, then add ours.
set -e
rm -f /etc/apt/sources.list.d/intel-gpu*.list \
      /etc/apt/sources.list.d/intel-graphics*.list \
      /usr/share/keyrings/intel-graphics-archive-keyring.gpg \
      /usr/share/keyrings/intel-graphics.gpg
wget -qO- https://repositories.intel.com/gpu/intel-graphics.key \
    | gpg --dearmor --output /usr/share/keyrings/intel-graphics.gpg
echo 'deb [arch=amd64,i386 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble unified' \
    > /etc/apt/sources.list.d/intel-gpu-noble.list
apt-get update
# The 2024 package rename: libze-intel-gpu1 replaces intel-level-zero-gpu
# (which has now-unresolvable deps on libigc1/libigdfcl1). Install the
# current names only.
apt-get install -y --no-install-recommends \
    intel-opencl-icd libze-intel-gpu1 libze1
# Fail fast if the GPU runtime didn't actually land (catches silent apt
# failures under apptainer's sh-based %post).
for _pkg in libze-intel-gpu1 libze1 intel-opencl-icd; do
  dpkg -s "$_pkg" >/dev/null 2>&1 || {
    echo "ERROR: required Intel GPU runtime package '$_pkg' missing after apt install" >&2
    exit 1
  }
done
ldconfig -p | grep -q 'libze_intel_gpu\.so\.1' || {
  echo "ERROR: libze_intel_gpu.so.1 not in ldconfig cache after install" >&2
  find /usr -name 'libze_intel_gpu*' 2>&1 >&2 || true
  exit 1
}
rm -rf /var/lib/apt/lists/*

# SSH setup (mirrors other builtin packages)
mkdir -p /var/run/sshd /root/.ssh \
    && ssh-keygen -A \
    && ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 \
    && cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys \
    && chmod 700 /root/.ssh \
    && chmod 600 /root/.ssh/authorized_keys \
    && sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\n" >> /etc/ssh/ssh_config

# Clone LAMMPS develop branch (upstream Kokkos SYCL support lands here first).
git clone --branch develop --depth 1 \
    https://github.com/lammps/lammps.git /opt/lammps

# MPI selection (critical on Aurora inside apptainer):
#   The intel/oneapi-hpckit image ships Intel MPI 2021.14 in PATH. Intel
#   MPI's mpi.h defines MPICH_NUMVERSION, which Kokkos' MPI detection
#   keys on to compile in MPICH-specific GPU-aware paths; at link time
#   against OpenMPI those symbols are missing. We force the container to
#   build and run against the apt-installed OpenMPI, which works fine
#   inside a rootless apptainer container.
unset I_MPI_ROOT I_MPI_OFI_LIBRARY_INTERNAL I_MPI_FABRICS I_MPI_PMI
unset MPI_ROOT MPI_HOME MPICC_PROFILE
# oneAPI's setvars.sh adds /opt/intel/oneapi/mpi/*/include to CPATH,
# which the preprocessor consults before cmake's explicit -I flags.
# With Intel MPI's mpi.h winning, Kokkos compiles in MPICH-only code
# paths that OpenMPI can't link. Scrub Intel MPI from CPATH,
# LIBRARY_PATH and LD_LIBRARY_PATH so OpenMPI's mpi.h is the one used.
_scrub() { echo "${1:-}" | tr ':' '\n' | grep -v '/opt/intel/oneapi/mpi/' | paste -sd: ; }
export CPATH="$(_scrub "${CPATH:-}")"
export LIBRARY_PATH="$(_scrub "${LIBRARY_PATH:-}")"
export LD_LIBRARY_PATH="$(_scrub "${LD_LIBRARY_PATH:-}")"
unset _scrub
export MPI_HOME=/usr/lib/x86_64-linux-gnu/openmpi
export CMAKE_PREFIX_PATH=/usr/lib/x86_64-linux-gnu/openmpi:${CMAKE_PREFIX_PATH:-}
# Verify /usr/bin/mpicxx is OpenMPI's, not Intel MPI's.
mpicxx_show=$(/usr/bin/mpicxx -show 2>&1 || true)
if echo "$mpicxx_show" | grep -q '/opt/intel/oneapi/mpi'; then
  echo "ERROR: /usr/bin/mpicxx is NOT the apt-installed OpenMPI wrapper" >&2
  echo "       (got: $mpicxx_show)" >&2
  exit 1
fi

# Build LAMMPS with Kokkos SYCL backend (JIT, not AOT).
#
# JIT vs AOT for Intel GPU:
#   - AOT (Kokkos_ARCH_INTEL_PVC=ON) emits `-fsycl-targets=spir64_gen
#     -device pvc`, which calls ocloc at build time. The oneapi-hpckit
#     image doesn't ship ocloc, so AOT fails.
#   - JIT (no Kokkos_ARCH_INTEL_*) emits `-fsycl-targets=spir64`,
#     compiling to portable SPIR-V. The Level Zero runtime translates
#     SPIR-V to native PVC code on first kernel launch and caches it.
#
# LAMMPS Kokkos builds require C++17 (icpx default) and will pick up
# its own bundled Kokkos unless EXTERNAL_KOKKOS is set.
cd /opt/lammps
mkdir -p build && cd build
#
# PKG_KSPACE is intentionally OFF for the SYCL build.
# fft3d_kokkos.cpp in LAMMPS's KSPACE package assumes either CUFFT or
# HIPFFT when the Kokkos device backend is a GPU — neither is present
# here, and Kokkos SYCL + KISS-FFT currently doesn't compile cleanly.
# The container's default benchmark is lj/cut (no long-range solver),
# so we don't need KSPACE. To enable it, add MKL GPU FFT support:
#   -DFFT_KOKKOS=MKL_GPU -DFFT_KOKKOS_MKL_GPU=ON
# and install appropriate oneAPI MKL dev packages.
cmake ../cmake \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_MPI=ON \
    -DCMAKE_CXX_COMPILER=icpx \
    -DCMAKE_C_COMPILER=icx \
    -DMPI_C_COMPILER=/usr/bin/mpicc \
    -DMPI_CXX_COMPILER=/usr/bin/mpicxx \
    -DMPIEXEC_EXECUTABLE=/usr/bin/mpiexec \
    -DPKG_KOKKOS=ON \
    -DKokkos_ENABLE_SYCL=ON \
    -DKokkos_ENABLE_OPENMP=OFF \
    -DKokkos_ENABLE_SERIAL=ON \
    -DPKG_MOLECULE=ON \
    -DPKG_KSPACE=OFF \
    -DPKG_RIGID=ON \
    -DCMAKE_DISABLE_FIND_PACKAGE_OpenMP=TRUE
make -j"${BUILD_JOBS:-4}"

# Sanity check: linked binary must not reference Intel MPI.
if ldd /opt/lammps/build/lmp 2>/dev/null \
     | grep -q '/opt/intel/oneapi/mpi/'; then
  echo "ERROR: lmp built against Intel MPI despite MPI_*_COMPILER overrides" >&2
  ldd /opt/lammps/build/lmp | grep -i mpi >&2
  exit 1
fi

ln -sf /opt/lammps/build/lmp /usr/local/bin/lmp

# Runtime env for every `apptainer exec/shell/run` invocation. Apptainer
# sources /.singularity.d/env/*.sh alphabetically at container entry;
# a 99-* script runs last and overrides the image's default ENV.
#
# Key constraints:
#   1. /usr/bin MUST precede /opt/intel/oneapi/mpi/... on PATH so the
#      apt-installed OpenMPI `mpiexec` is found (not Intel MPI's Hydra,
#      which doesn't work in a rootless apptainer).
#   2. LD_LIBRARY_PATH must include the icpx SYCL + Level Zero runtime
#      dirs so libsycl.so.8 and libur_loader.so.0 are resolvable at
#      lmp startup.
#   3. ONEAPI_DEVICE_SELECTOR=level_zero:gpu pins Level Zero GPU devices
#      so Kokkos SYCL selects the PVC Level Zero backend, not OpenCL.
#   4. OMPI_MCA_btl_vader_single_copy_mechanism=none disables CMA
#      (process_vm_readv) intra-node transfer. CMA needs
#      PTRACE_MODE_ATTACH_REALCREDS which fails under apptainer's
#      unprivileged userns, producing `Read -1, expected N, errno = 14`
#      log spam. The fallback shmem copy-in/copy-out path is correct.
mkdir -p /.singularity.d/env
cat > /.singularity.d/env/99-sycl.sh <<'EOF'
export PATH="/usr/bin:/opt/lammps/build:/opt/intel/oneapi/compiler/2025.0/bin:/opt/intel/oneapi/mkl/2025.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin"
export LD_LIBRARY_PATH="/opt/intel/oneapi/compiler/2025.0/lib:/opt/intel/oneapi/mkl/2025.0/lib:/opt/intel/oneapi/tbb/2025.0/lib/intel64/gcc4.8:${LD_LIBRARY_PATH:-}"
export LIBRARY_PATH="/opt/intel/oneapi/compiler/2025.0/lib:${LIBRARY_PATH:-}"
export ONEAPI_DEVICE_SELECTOR=level_zero:gpu
export OMPI_MCA_btl_vader_single_copy_mechanism=none
EOF
chmod +x /.singularity.d/env/99-sycl.sh

# Propagate the same env via sshd SetEnv for SSH-based multi-node runs
# (jarvis's Apptainer deploy mode opens an SSH session into the
# container instance on each remote host; that session is launched by
# sshd with UsePAM=no, which strips /etc/environment and ~/.profile).
cat > /etc/ssh/sshd_config.d/99-sycl.conf <<'EOF'
SetEnv PATH=/usr/bin:/opt/lammps/build:/opt/intel/oneapi/compiler/2025.0/bin:/opt/intel/oneapi/mkl/2025.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin
SetEnv LD_LIBRARY_PATH=/opt/intel/oneapi/compiler/2025.0/lib:/opt/intel/oneapi/mkl/2025.0/lib:/opt/intel/oneapi/tbb/2025.0/lib/intel64/gcc4.8
SetEnv ONEAPI_DEVICE_SELECTOR=level_zero:gpu
SetEnv OMPI_MCA_btl_vader_single_copy_mechanism=none
EOF

export PATH=/opt/lammps/build:${PATH}

#!/bin/bash
# Build Nyx (AMReX HydroTests) with Intel SYCL backend (JIT mode).
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
#
# The kernel i915 driver <-> user-mode runtime ABI is stable across
# versions, so a container-shipped Level Zero 1.3.x runtime works fine
# with Aurora's host i915 kernel driver (1.6.x). No host library binds
# are needed at run time. See apptainer/apptainer#1592.
apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential gfortran gnupg wget \
    openmpi-bin libopenmpi-dev \
    openssh-server openssh-client

# Intel GPU user-mode runtime (Level Zero + OpenCL) from Intel's APT repo.
# Ubuntu 24.04's universe repo does not ship these packages.
#
# CAREFUL: the intel/oneapi-hpckit base image ALREADY configures the Intel
# GPU apt repo, but with a different keyring path
# (/usr/share/keyrings/intel-graphics-archive-keyring.gpg). If we add our
# own entry at a different path, `apt-get update` errors with:
#   E: Conflicting values set for option Signed-By regarding source
#      https://repositories.intel.com/gpu/ubuntu/ noble
#   E: The list of sources could not be read.
# ...and subsequent apt-get install for Intel-GPU packages is silently
# skipped — apptainer's %post runs under /bin/sh, which does NOT honor
# the `set -e` from the script's #!/bin/bash header, so this failure
# was previously silent.
#
# Fix: explicitly purge any pre-existing Intel GPU repo entries (both
# sources.list.d files and their keyrings) before adding our own single
# entry. Then verify the GPU packages actually landed.
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
# Intel renamed the Level Zero GPU backend packages in early 2024:
#   OLD (pre-2024): intel-level-zero-gpu  (dep: libigc1,  libigdfcl1)
#   NEW (current):  libze-intel-gpu1      (dep: libigc2,  libigdfcl2)
# Intel's current noble/unified repo ships only the NEW packages; the
# OLD `intel-level-zero-gpu` meta is still listed but its dependencies
# were dropped, so installing it fails with "unmet dependencies".
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
# Sanity: the Level Zero GPU backend .so must be on the image.
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

# Clone Nyx with AMReX submodule. AMReX 'development' branch carries the
# most up-to-date SYCL support.
git clone --recursive https://github.com/AMReX-Astro/Nyx.git /opt/Nyx \
    && cd /opt/Nyx/subprojects/amrex && git checkout development

# Patch Nyx's CMakeLists.txt: Nyx historically force-disables MPI when
# Nyx_GPU_BACKEND=SYCL via cmake_dependent_option:
#
#   cmake_dependent_option( Nyx_MPI  "Enable MPI"  ON
#      "NOT Nyx_GPU_BACKEND STREQUAL SYCL" OFF)
#
# This cascades into AMReX_MPI=OFF (see NyxSetupAMReX.cmake line 103,
# `set(AMReX_MPI ${Nyx_MPI} ...)`), producing a non-MPI binary even when
# -DNyx_MPI=YES is passed on the command line — cmake_dependent_option's
# FORCE semantics override user -D flags.
#
# Modern AMReX SYCL + OpenMPI works fine on Intel PVC, so drop the
# SYCL dependency and make Nyx_MPI a plain option defaulting to ON.
cd /opt/Nyx
python3 - <<'PY'
import re, pathlib
p = pathlib.Path('/opt/Nyx/CMakeLists.txt')
src = p.read_text()
new = re.sub(
    r'cmake_dependent_option\(\s*Nyx_MPI\s+"Enable MPI"\s+ON\s*\n\s*"NOT Nyx_GPU_BACKEND STREQUAL SYCL"\s+OFF\)',
    'option( Nyx_MPI  "Enable MPI"  ON )',
    src,
)
if new == src:
    raise SystemExit("ERROR: Nyx_MPI cmake_dependent_option pattern not found for patching")
p.write_text(new)
PY
grep -n 'Nyx_MPI' /opt/Nyx/CMakeLists.txt | head -3

# MPI selection (critical on Aurora inside apptainer):
#   The intel/oneapi-hpckit image ships Intel MPI 2021.14 in PATH. CMake's
#   FindMPI would pick Intel MPI's mpicxx by default, linking against
#   /opt/intel/oneapi/mpi/2021.14/lib/libmpi.so.12. At runtime inside a
#   rootless apptainer container, Intel MPI fails because:
#     a) Its open_fabric() needs a libfabric OFI provider — Aurora's
#        /opt/cray/libfabric is on the host only, not in the container.
#     b) Its Hydra launcher auto-detects PBS and tries to read
#        $PBS_NODEFILE which doesn't exist inside the container.
#   Force CMake to use the apt-installed OpenMPI instead. OpenMPI bundles
#   PMIx and uses BTL shared-memory on one node, so it works in a plain
#   rootless apptainer container with zero host binds.
unset I_MPI_ROOT I_MPI_OFI_LIBRARY_INTERNAL I_MPI_FABRICS I_MPI_PMI
unset MPI_ROOT MPI_HOME MPICC_PROFILE
# oneAPI's setvars.sh adds /opt/intel/oneapi/mpi/*/include to CPATH, which
# beats CMake's explicit -I/usr/.../openmpi/include (CPATH is searched
# before explicit -I by the preprocessor). With Intel MPI's mpi.h,
# MPICH_NUMVERSION>=40000000 is defined, causing AMReX's
# ParallelDescriptor to compile a call to MPIX_GPU_query_support —
# a MPICH 4.0+ GPU-aware MPI extension absent from OpenMPI. At link
# time we'd get: "undefined reference to MPIX_GPU_query_support".
# Remove every Intel MPI entry from CPATH / LIBRARY_PATH / LD_LIBRARY_PATH
# so the compiler sees only OpenMPI's mpi.h.
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

# Build Nyx HydroTests with AMReX SYCL backend (JIT, not AOT).
#
# JIT vs AOT for Intel GPU:
#   - AOT (AMReX_SYCL_AOT=YES + AMReX_INTEL_ARCH=pvc) calls ocloc at
#     build time to generate PVC-specific GPU code. The oneapi-hpckit
#     image ships icpx but NOT ocloc/llvm-foreach, so AOT fails.
#   - JIT (default) compiles to portable SPIR-V bitcode; the Level Zero
#     runtime translates SPIR-V to native PVC code on first kernel
#     launch and caches the result.
#
# OpenMP is suppressed at two layers:
#   1. -DNyx_OMP=NO tells AMReX not to enable OpenMP, so AMReX's CMake
#      does not call find_package(OpenMP) internally.
#   2. -DCMAKE_DISABLE_FIND_PACKAGE_OpenMP=TRUE is a belt-and-suspenders
#      guard: if any submodule calls find_package(OpenMP) anyway, it
#      returns "not found" instead of injecting -fiopenmp. -fiopenmp
#      combined with SYCL flags breaks AMReX's compiler flag check.
cd /opt/Nyx
cmake -S . -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DNyx_MPI=YES \
    -DNyx_OMP=NO \
    -DNyx_HYDRO=YES \
    -DNyx_HEATCOOL=NO \
    -DNyx_GPU_BACKEND=SYCL \
    -DAMReX_GPU_BACKEND=SYCL \
    -DAMReX_PRECISION=SINGLE \
    -DAMReX_PARTICLES_PRECISION=SINGLE \
    -DCMAKE_CXX_COMPILER=icpx \
    -DMPI_C_COMPILER=/usr/bin/mpicc \
    -DMPI_CXX_COMPILER=/usr/bin/mpicxx \
    -DMPIEXEC_EXECUTABLE=/usr/bin/mpiexec \
    -DMPI_HOME=/usr/lib/x86_64-linux-gnu/openmpi \
    -DCMAKE_DISABLE_FIND_PACKAGE_OpenMP=TRUE
cmake --build build --target nyx_HydroTests -j"${BUILD_JOBS:-4}"

# Sanity check: linked binary must not reference Intel MPI.
if ldd /opt/Nyx/build/Exec/HydroTests/nyx_HydroTests 2>/dev/null \
     | grep -q '/opt/intel/oneapi/mpi/'; then
  echo "ERROR: nyx_HydroTests built against Intel MPI despite MPI_*_COMPILER overrides" >&2
  ldd /opt/Nyx/build/Exec/HydroTests/nyx_HydroTests | grep -i mpi >&2
  exit 1
fi

ln -sf /opt/Nyx/build/Exec/HydroTests/nyx_HydroTests /usr/bin/nyx_HydroTests

# Runtime env for EVERY `apptainer exec/shell/run` invocation. Apptainer
# sources /.singularity.d/env/*.sh alphabetically at container entry; a
# 99-* script runs last and can override the image's default ENV.
#
# Key constraints:
#   1. /usr/bin MUST precede /opt/intel/oneapi/mpi/... on PATH so the
#      apt-installed OpenMPI `mpiexec` is found (not Intel MPI's Hydra,
#      which doesn't work in a rootless apptainer).
#   2. LD_LIBRARY_PATH must include the icpx SYCL + Level Zero runtime
#      dirs so libsycl.so.8, libur_loader.so.0, and libiomp5.so are
#      resolvable at Nyx startup.
#   3. ONEAPI_DEVICE_SELECTOR=level_zero:gpu pins the Level Zero GPU
#      devices and hides the OpenCL CPU/GPU aliases so AMReX selects
#      the Level Zero PVC device, not the OpenCL alias (which has
#      known PVC JIT issues).
#
# SYCL_DEVICE_FILTER is intentionally NOT set: deprecated in DPC++ 2024+.
mkdir -p /.singularity.d/env
cat > /.singularity.d/env/99-sycl.sh <<'EOF'
export PATH="/usr/bin:/opt/Nyx/build/Exec/HydroTests:/opt/intel/oneapi/compiler/2025.0/bin:/opt/intel/oneapi/mkl/2025.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin"
export LD_LIBRARY_PATH="/opt/intel/oneapi/compiler/2025.0/lib:/opt/intel/oneapi/mkl/2025.0/lib:/opt/intel/oneapi/tbb/2025.0/lib/intel64/gcc4.8:${LD_LIBRARY_PATH:-}"
export LIBRARY_PATH="/opt/intel/oneapi/compiler/2025.0/lib:${LIBRARY_PATH:-}"
export ONEAPI_DEVICE_SELECTOR=level_zero:gpu
# Disable OpenMPI's CMA (process_vm_readv) single-copy intra-node
# transfer. CMA requires PTRACE_MODE_ATTACH_REALCREDS which fails under
# apptainer's unprivileged user namespace, printing `Read -1, expected
# N, errno = 14` for every intra-node transfer. OpenMPI falls back to
# plain shmem copy-in/copy-out so runs complete correctly but the log
# is flooded. Using `none` forces the fallback up front.
export OMPI_MCA_btl_vader_single_copy_mechanism=none
EOF
chmod +x /.singularity.d/env/99-sycl.sh

# Propagate the same env via sshd SetEnv for SSH-based multi-node runs
# (jarvis's Apptainer deploy mode opens an SSH session into the container
# instance on each remote host; that session is launched by sshd with
# UsePAM=no, which strips /etc/environment and ~/.profile).
cat > /etc/ssh/sshd_config.d/99-sycl.conf <<'EOF'
SetEnv PATH=/usr/bin:/opt/Nyx/build/Exec/HydroTests:/opt/intel/oneapi/compiler/2025.0/bin:/opt/intel/oneapi/mkl/2025.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin
SetEnv LD_LIBRARY_PATH=/opt/intel/oneapi/compiler/2025.0/lib:/opt/intel/oneapi/mkl/2025.0/lib:/opt/intel/oneapi/tbb/2025.0/lib/intel64/gcc4.8
SetEnv ONEAPI_DEVICE_SELECTOR=level_zero:gpu
SetEnv OMPI_MCA_btl_vader_single_copy_mechanism=none
EOF

export PATH=/opt/Nyx/build/Exec/HydroTests:${PATH}

"""
Container-based LAMMPS deployment using Docker/Podman/Apptainer.
Builds LAMMPS with Kokkos CUDA from the develop branch.
"""
from jarvis_cd.core.container_pkg import ContainerApplication


class LammpsContainer(ContainerApplication):
    """
    Container-based LAMMPS deployment.

    Build phase: Clones LAMMPS develop branch and builds with Kokkos CUDA.
    Deploy phase: Copies the lmp binary to a leaner runtime image.

    Based on: awesome-scienctific-applications/lammps/Dockerfile
    """

    def _build_phase(self) -> str:
        """
        Build container: full LAMMPS build with Kokkos CUDA.
        Uses Git layer cache — clone is cached until URL/branch changes.
        """
        cuda_arch = self.config.get('cuda_arch', 80)
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {base}

ARG CUDA_ARCH={cuda_arch}

# Clone LAMMPS develop branch (cached unless branch changes)
RUN git clone --branch develop --depth 1 \\
    https://github.com/lammps/lammps.git /opt/lammps

# Build LAMMPS with Kokkos CUDA
# Ordered to maximize Docker layer cache reuse:
# cmake configure → make (expensive, cached when source unchanged)
RUN cd /opt/lammps \\
    && mkdir -p build && cd build \\
    && cmake ../cmake \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DPKG_KOKKOS=ON \\
        -DKokkos_ENABLE_CUDA=ON \\
        "-DKokkos_ARCH_AMPERE${{CUDA_ARCH}}=ON" \\
        -DBUILD_MPI=ON \\
        -DPKG_MOLECULE=ON \\
        -DPKG_KSPACE=ON \\
        -DPKG_RIGID=ON \\
    && make -j$(nproc)

ENV PATH=/opt/lammps/build:${{PATH}}
"""

    def _build_deploy_phase(self) -> str:
        """
        Deploy container: copies lmp binary from build container.
        Much faster to build than the full compile.
        """
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

# Copy compiled LAMMPS binary from build container
COPY --from=builder /opt/lammps/build/lmp /usr/bin/lmp

ENV PATH=/usr/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    def augment_container(self) -> str:
        """Legacy pipeline-level Dockerfile commands for LAMMPS."""
        cuda_arch = self.config.get('cuda_arch', 80)
        return f"""
# Clone and build LAMMPS with Kokkos CUDA
RUN git clone --branch develop --depth 1 \\
    https://github.com/lammps/lammps.git /opt/lammps && \\
    cd /opt/lammps && \\
    mkdir -p build && cd build && \\
    cmake ../cmake \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DPKG_KOKKOS=ON \\
        -DKokkos_ENABLE_CUDA=ON \\
        -DKokkos_ARCH_AMPERE{cuda_arch}=ON \\
        -DBUILD_MPI=ON \\
        -DPKG_MOLECULE=ON \\
        -DPKG_KSPACE=ON \\
        -DPKG_RIGID=ON \\
    && make -j$(nproc) && \\
    cp /opt/lammps/build/lmp /usr/bin/lmp

ENV PATH=/usr/bin:${{PATH}}
"""

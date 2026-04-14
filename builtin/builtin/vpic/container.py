"""
Container-based VPIC-Kokkos deployment using Docker/Podman/Apptainer.
Builds VPIC with Kokkos CUDA backend.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from jarvis_cd.shell import Exec


class VpicContainer(ContainerApplication):
    """
    Container-based VPIC-Kokkos deployment.

    Build phase: Clones vpic-kokkos and builds with CUDA.
    Deploy phase: Copies vpic binary and samples to a leaner runtime image.

    Based on: awesome-scienctific-applications/vpic/Dockerfile
    """

    def _build_phase(self) -> str:
        cuda_arch = self.config.get('cuda_arch', 80)
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {base}

ARG CUDA_ARCH={cuda_arch}

# Clone VPIC-Kokkos with bundled Kokkos (cached until repo changes)
RUN git clone --recursive https://github.com/lanl/vpic-kokkos.git /opt/vpic-kokkos

# Build VPIC core library with Kokkos CUDA
RUN cd /opt/vpic-kokkos \\
    && cmake -S . -B build \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DENABLE_KOKKOS_CUDA=ON \\
        -DBUILD_INTERNAL_KOKKOS=ON \\
        "-DKokkos_ARCH_AMPERE${{CUDA_ARCH}}=ON" \\
        -DCMAKE_CXX_COMPILER="$(pwd)/kokkos/bin/nvcc_wrapper" \\
    && cmake --build build -j$(nproc)

# Patch deck-compiler to link against CUDA stub library
RUN sed -i \\
    's|-lkokkossimd|-lkokkossimd -L/usr/local/cuda/lib64/stubs -lcuda|' \\
    /opt/vpic-kokkos/build/bin/vpic

ENV NVCC_WRAPPER_DEFAULT_COMPILER=g++
ENV PATH=/opt/vpic-kokkos/build/bin:${{PATH}}
"""

    def _build_deploy_phase(self) -> str:
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

# Copy VPIC binaries and samples from build container
COPY --from=builder /opt/vpic-kokkos/build/bin /opt/vpic-kokkos/build/bin
COPY --from=builder /opt/vpic-kokkos/sample /opt/vpic-kokkos/sample
COPY --from=builder /opt/vpic-kokkos/kokkos/bin/nvcc_wrapper /opt/vpic-kokkos/kokkos/bin/nvcc_wrapper

ENV NVCC_WRAPPER_DEFAULT_COMPILER=g++
ENV PATH=/opt/vpic-kokkos/build/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    def start(self):
        run_dir = self.config['run_dir']
        from jarvis_cd.shell.process import Mkdir
        Mkdir(run_dir).run()

        if self.config.get('deck'):
            deck_file = self.config['deck']
        else:
            sample = self.config.get('sample_deck', 'harris')
            deck_file = f'/opt/vpic-kokkos/sample/{sample}/{sample}.cxx'

        import os
        deck_name = os.path.basename(deck_file).replace('.cxx', '')

        nprocs = self.config.get('nprocs', 4)

        # Step 1: Compile deck inside container
        compile_cmd = f'bash -c "cp {deck_file} {run_dir}/ && cd {run_dir} && /opt/vpic-kokkos/build/bin/vpic {deck_name}.cxx"'
        Exec(compile_cmd, self.container_exec_info(gpu=True)).run()

        # Step 2: Run compiled binary inside container
        run_cmd = f'bash -c "cd {run_dir} && mpirun --allow-run-as-root -n {nprocs} {run_dir}/{deck_name}.Linux"'
        Exec(run_cmd, self.container_exec_info(gpu=True)).run()

    def clean(self):
        from jarvis_cd.shell.process import Rm
        Rm(self.config.get('run_dir', '') + '*').run()

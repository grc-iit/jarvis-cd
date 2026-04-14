"""
Container-based Nyx cosmological simulation using Docker/Podman/Apptainer.
Builds Nyx HydroTests target with CUDA, MPI, and HDF5.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from jarvis_cd.shell import Exec, LocalExecInfo


class NyxContainer(ContainerApplication):
    """
    Container-based Nyx AMReX cosmological simulation.

    Build phase: Clones Nyx with AMReX submodule and builds HydroTests.
    Deploy phase: Copies binary to a leaner runtime image.

    Based on: awesome-scienctific-applications/nyx/Dockerfile
    """

    def _build_phase(self) -> str:
        cuda_arch = self.config.get('cuda_arch', 80)
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {base}

ARG CUDA_ARCH={cuda_arch}

# Clone Nyx with AMReX submodule (cached until repo changes)
RUN git clone --recursive https://github.com/AMReX-Astro/Nyx.git /opt/Nyx \\
    && cd /opt/Nyx/subprojects/amrex && git checkout development

# Build Nyx HydroTests — single precision, CUDA, MPI, HDF5
# Ordered for maximum cache reuse: configure then build
RUN cd /opt/Nyx \\
    && cmake -S . -B build \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DNyx_MPI=YES \\
        -DNyx_OMP=NO \\
        -DNyx_HYDRO=YES \\
        -DNyx_HEATCOOL=NO \\
        -DAMReX_HDF5=YES \\
        -DHDF5_ROOT=/opt/hdf5 \\
        -DNyx_GPU_BACKEND=CUDA \\
        "-DAMReX_CUDA_ARCH=${{CUDA_ARCH}}" \\
        -DAMReX_PRECISION=SINGLE \\
        -DAMReX_PARTICLES_PRECISION=SINGLE \\
        -DCMAKE_C_COMPILER="$(which gcc)" \\
        -DCMAKE_CXX_COMPILER="$(which g++)" \\
        -DCMAKE_CUDA_HOST_COMPILER="$(which g++)" \\
    && cmake --build build --target nyx_HydroTests -j$(nproc)

ENV PATH=/opt/Nyx/build/Exec/HydroTests:${{PATH}}
"""

    def _build_deploy_phase(self) -> str:
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

# Copy Nyx HydroTests binary from build container
COPY --from=builder /opt/Nyx/build/Exec/HydroTests/nyx_HydroTests /usr/bin/nyx_HydroTests

ENV PATH=/usr/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    def start(self):
        outdir = self.config.get('out', '/tmp/nyx_out')
        from jarvis_cd.shell.process import Mkdir
        Mkdir(outdir).run()

        nprocs = self.config.get('nprocs', 4)
        inner = ' '.join([
            f'mpirun --allow-run-as-root -n {nprocs}',
            '/usr/bin/nyx_HydroTests',
            f'max_step={self.config["max_step"]}',
            f'amr.n_cell={self.config["n_cell"]}',
            f'amr.max_level={self.config["max_level"]}',
            f'amr.plot_file={outdir}/plt',
            f'amr.plot_int={self.config["plot_int"]}',
        ])
        Exec(self.wrap_container_cmd(inner, gpu=True), LocalExecInfo()).run()

    def clean(self):
        from jarvis_cd.shell.process import Rm
        Rm(self.config.get('out', '') + '*').run()

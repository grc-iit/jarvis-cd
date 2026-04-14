"""
Container-based WarpX deployment using Docker/Podman/Apptainer.
Builds WarpX 3D CUDA+MPI+HDF5 from the BLAST-WarpX repository.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from jarvis_cd.shell import Exec


class WarpxContainer(ContainerApplication):
    """
    Container-based WarpX deployment.

    Build phase: Clones WarpX and builds 3D CUDA+MPI configuration.
    Deploy phase: Copies warpx binary to a leaner runtime image.

    Based on: awesome-scienctific-applications/warpx/Dockerfile
    """

    def _build_phase(self) -> str:
        cuda_arch = self.config.get('cuda_arch', 80)
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {base}

ARG CUDA_ARCH={cuda_arch}

# Clone WarpX (cached at this layer until URL changes)
RUN git clone https://github.com/BLAST-WarpX/warpx.git /opt/warpx

# Build WarpX 3D CUDA+MPI+HDF5
# Ordered for maximum layer cache reuse: cmake configure then make
RUN cd /opt/warpx \\
    && mkdir -p build && cd build \\
    && CC=$(which gcc) CXX=$(which g++) CUDACXX=$(which nvcc) CUDAHOSTCXX=$(which g++) \\
       cmake -S .. -B . \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DWarpX_COMPUTE=CUDA \\
        -DWarpX_MPI=ON \\
        -DWarpX_DIMS=3 \\
        -DWarpX_PRECISION=SINGLE \\
        -DWarpX_PARTICLE_PRECISION=SINGLE \\
        -DAMReX_HDF5=YES \\
        "-DCMAKE_PREFIX_PATH=/opt/hdf5" \\
        "-DAMReX_CUDA_ARCH=${{CUDA_ARCH}}" \\
        "-DCMAKE_CXX_FLAGS=-mcmodel=large" \\
        "-DCMAKE_CUDA_FLAGS=-Xcompiler -mcmodel=large --diag-suppress=222 --diag-suppress=221" \\
    && cmake --build . -j$(nproc)

ENV PATH=/opt/warpx/build/bin:${{PATH}}
"""

    def _build_deploy_phase(self) -> str:
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

# Copy WarpX binary and example inputs from build container
COPY --from=builder /opt/warpx/build/bin/warpx.3d.MPI.CUDA.SP.PSP.OPMD.EB.QED /usr/bin/warpx
COPY --from=builder /opt/warpx/Examples /opt/warpx/Examples

ENV PATH=/usr/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    def start(self):
        outdir = self.config.get('out', '/tmp/warpx_out')
        from jarvis_cd.shell.process import Mkdir
        Mkdir(outdir).run()

        nprocs = self.config.get('nprocs', 2)

        if self.config.get('inputs'):
            inputs_arg = self.config['inputs']
        else:
            example = self.config.get('example', 'laser_acceleration')
            inputs_arg = f'/opt/warpx/Examples/Physics_applications/{example}/inputs_base_3d'

        inner = ' '.join([
            f'mpirun --allow-run-as-root -n {nprocs}',
            '/usr/bin/warpx',
            inputs_arg,
            f'max_step={self.config["max_step"]}',
            f'amr.n_cell={self.config["n_cell"]}',
            f'amr.plot_file={outdir}/plt',
            f'amr.plot_int={self.config["plot_int"]}',
        ])
        Exec(inner, self.container_exec_info(gpu=True)).run()

    def clean(self):
        from jarvis_cd.shell.process import Rm
        Rm(self.config.get('out', '') + '*').run()

"""
Container-based WarpX deployment using Docker/Podman/Apptainer.
Builds WarpX 3D CUDA+MPI+HDF5 from the BLAST-WarpX repository.
"""
from jarvis_cd.core.container_pkg import ContainerApplication
from jarvis_cd.shell import Exec, MpiExecInfo
from jarvis_cd.shell.process import Mkdir


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
COPY --from=builder /opt/warpx/build/bin/warpx.3d.MPI.CUDA.SP /usr/bin/warpx.3d.MPI.CUDA.SP
COPY --from=builder /opt/warpx/Examples /opt/warpx/Examples

ENV PATH=/usr/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    def augment_container(self) -> str:
        cuda_arch = self.config.get('cuda_arch', 80)
        return f"""
# Build WarpX 3D CUDA+MPI+HDF5
RUN git clone https://github.com/BLAST-WarpX/warpx.git /opt/warpx && \\
    cd /opt/warpx && \\
    mkdir -p build && cd build && \\
    CC=$(which gcc) CXX=$(which g++) CUDACXX=$(which nvcc) CUDAHOSTCXX=$(which g++) \\
    cmake -S .. -B . \\
        -DCMAKE_BUILD_TYPE=Release \\
        -DWarpX_COMPUTE=CUDA \\
        -DWarpX_MPI=ON \\
        -DWarpX_DIMS=3 \\
        -DWarpX_PRECISION=SINGLE \\
        -DWarpX_PARTICLE_PRECISION=SINGLE \\
        -DAMReX_HDF5=YES \\
        -DCMAKE_PREFIX_PATH=/opt/hdf5 \\
        -DAMReX_CUDA_ARCH={cuda_arch} \\
        "-DCMAKE_CXX_FLAGS=-mcmodel=large" \\
        "-DCMAKE_CUDA_FLAGS=-Xcompiler -mcmodel=large --diag-suppress=222 --diag-suppress=221" \\
    && cmake --build . -j$(nproc) && \\
    cp /opt/warpx/build/bin/warpx.3d.MPI.CUDA.SP /usr/bin/

ENV PATH=/usr/bin:${{PATH}}
"""

    def start(self):
        outdir = self.config.get('out', '/tmp/warpx_out')
        Mkdir(outdir).run()

        if self.config.get('inputs'):
            import os
            cwd = os.path.dirname(self.config['inputs'])
            inputs_arg = self.config['inputs']
        else:
            example = self.config.get('example', 'laser_acceleration')
            cwd = f'/opt/warpx/Examples/Physics_applications/{example}'
            inputs_arg = 'inputs_base_3d'

        cmd = [
            '/usr/bin/warpx.3d.MPI.CUDA.SP',
            inputs_arg,
            f'max_step={self.config["max_step"]}',
            f'amr.n_cell={self.config["n_cell"]}',
            f'amr.plot_file={outdir}/plt',
            f'amr.plot_int={self.config["plot_int"]}',
        ]

        Exec(' '.join(cmd),
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.hostfile,
                         env=self.mod_env,
                         cwd=cwd)).run()

    def clean(self):
        from jarvis_cd.shell.process import Rm
        Rm(self.config.get('out', '') + '*').run()

"""
WarpX — Exascale Particle-In-Cell plasma accelerator simulation.
Highly parallel, GPU-optimized PIC code built on AMReX.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
import os
import shutil


class Warpx(Application):
    """
    Merged WarpX class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run WarpX inside a Docker/Podman/Apptainer
    container with 3D CUDA+MPI+HDF5.  Set deploy_mode='default' to use a
    system-installed warpx binary via MPI.
    """

    def _init(self):
        self.warpx_bin = None

    def _configure_menu(self):
        return [
            {
                'name': 'deploy_mode',
                'msg': 'Deployment mode',
                'type': str,
                'choices': ['default', 'container'],
                'default': 'default',
            },
            {
                'name': 'nprocs',
                'msg': 'Number of MPI processes',
                'type': int,
                'default': 2,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 2,
            },
            {
                'name': 'inputs',
                'msg': 'Path to WarpX inputs file',
                'type': str,
                'default': None,
            },
            {
                'name': 'example',
                'msg': 'Built-in example to run (e.g., laser_acceleration, uniform_plasma)',
                'type': str,
                'choices': ['laser_acceleration', 'uniform_plasma', 'custom'],
                'default': 'laser_acceleration',
            },
            {
                'name': 'max_step',
                'msg': 'Total number of time steps',
                'type': int,
                'default': 50,
            },
            {
                'name': 'n_cell',
                'msg': 'Base grid cells as "nx ny nz"',
                'type': str,
                'default': '64 64 128',
            },
            {
                'name': 'out',
                'msg': 'Output directory for plot files',
                'type': str,
                'default': '/tmp/warpx_out',
            },
            {
                'name': 'plot_int',
                'msg': 'Plot output interval (-1 to disable)',
                'type': int,
                'default': 10,
            },
            {
                'name': 'cuda_arch',
                'msg': 'CUDA architecture code (80=A100, 90=H100, 70=V100)',
                'type': int,
                'default': 80,
            },
            {
                'name': 'base_image',
                'msg': 'Base Docker image for build container',
                'type': str,
                'default': 'sci-hpc-base',
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self) -> str:
        """
        Return the BUILD container Dockerfile, or None when not in container mode.
        """
        if self.config.get('deploy_mode') != 'container':
            return None
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
        """
        Return the DEPLOY container Dockerfile, or None when not in container mode.
        """
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gdb gdbserver \\
    && rm -rf /var/lib/apt/lists/*

# Copy WarpX binary and example inputs from build container
COPY --from=builder /opt/warpx/build/bin/warpx.3d.MPI.CUDA.SP.PSP.OPMD.EB.QED /usr/bin/warpx
COPY --from=builder /opt/warpx/Examples /opt/warpx/Examples

ENV PATH=/usr/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure WarpX.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory and locates the
        warpx binary on the system PATH.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            Mkdir(self.config['out'],
                  PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
            # Find warpx binary (various naming conventions)
            for name in ['warpx.3d.MPI.CUDA.SP', 'warpx', 'warpx.3d']:
                if shutil.which(name):
                    self.warpx_bin = name
                    break
            if not self.warpx_bin:
                self.warpx_bin = 'warpx.3d.MPI.CUDA.SP'

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch WarpX.

        Branches on deploy_mode: uses container_exec_info() for container
        mode, MpiExecInfo with hostfile for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            outdir = self.config.get('out', '/tmp/warpx_out')
            Mkdir(outdir).run()

            nprocs = self.config.get('nprocs', 2)

            if self.config.get('inputs'):
                inputs_arg = self.config['inputs']
            else:
                example = self.config.get('example', 'laser_acceleration')
                inputs_arg = (
                    f'/opt/warpx/Examples/Physics_applications/{example}/inputs_base_3d'
                )

            inner = ' '.join([
                '/usr/bin/warpx',
                inputs_arg,
                f'max_step={self.config["max_step"]}',
                f'amr.n_cell={self.config["n_cell"]}',
                f'amr.plot_file={outdir}/plt',
                f'amr.plot_int={self.config["plot_int"]}',
            ])
            Exec(inner, MpiExecInfo(
                nprocs=nprocs,
                container=self._container_engine,
                container_image=self.deploy_image_name,
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                gpu=True,
                env=self.mod_env,
            )).run()
        else:
            if self.config['inputs']:
                inputs_arg = self.config['inputs']
                cwd = os.path.dirname(self.config['inputs'])
            elif self.config['example'] != 'custom':
                example_dir = (
                    f'/opt/warpx/Examples/Physics_applications/{self.config["example"]}'
                )
                inputs_arg = 'inputs_base_3d'
                cwd = example_dir
            else:
                raise ValueError("Either 'inputs' or 'example' must be specified")

            cmd = [
                self.warpx_bin,
                inputs_arg,
                f'max_step={self.config["max_step"]}',
                f'amr.n_cell={self.config["n_cell"]}',
                f'amr.plot_file={self.config["out"]}/plt',
                f'amr.plot_int={self.config["plot_int"]}',
            ]

            Exec(' '.join(cmd),
                 MpiExecInfo(nprocs=self.config['nprocs'],
                             ppn=self.config['ppn'],
                             hostfile=self.hostfile,
                             env=self.mod_env,
                             cwd=cwd)).run()

    def stop(self):
        """Stop WarpX (no-op — WarpX runs to completion)."""
        pass

    def clean(self):
        """Remove WarpX output directory."""
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

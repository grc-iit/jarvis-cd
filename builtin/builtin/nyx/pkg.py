"""
Nyx — AMReX-based cosmological simulation (HydroTests).
Adaptive mesh, massively parallel simulation code from AMReX-Astro.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Nyx(Application):
    """
    Merged Nyx class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run Nyx inside a Docker/Podman/Apptainer
    container with CUDA+MPI+HDF5.  Set deploy_mode='default' to use a
    system-installed nyx_HydroTests binary via MPI.
    """

    def _init(self):
        pass

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
                'default': 4,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 4,
            },
            {
                'name': 'max_step',
                'msg': 'Number of coarse time steps',
                'type': int,
                'default': 100,
            },
            {
                'name': 'n_cell',
                'msg': 'Base grid cells as "nx ny nz"',
                'type': str,
                'default': '128 128 128',
            },
            {
                'name': 'max_level',
                'msg': 'Maximum AMR refinement level',
                'type': int,
                'default': 0,
            },
            {
                'name': 'out',
                'msg': 'Output directory for plot files',
                'type': str,
                'default': '/tmp/nyx_out',
            },
            {
                'name': 'plot_int',
                'msg': 'Plot file interval (-1 to disable)',
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
        """
        Return the DEPLOY container Dockerfile, or None when not in container mode.
        """
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        return f"""FROM {self.build_image_name} AS builder
FROM {base}

# Copy Nyx HydroTests binary from build container
COPY --from=builder /opt/Nyx/build/Exec/HydroTests/nyx_HydroTests /usr/bin/nyx_HydroTests

ENV PATH=/usr/bin:${{PATH}}

CMD ["/bin/bash"]
"""

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure Nyx.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            Mkdir(self.config['out'],
                  PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch Nyx.

        Branches on deploy_mode: uses container_exec_info() for container
        mode, MpiExecInfo with hostfile for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            outdir = self.config.get('out', '/tmp/nyx_out')
            Mkdir(outdir).run()

            nprocs = self.config.get('nprocs', 4)
            inner = ' '.join([
                '/usr/bin/nyx_HydroTests',
                f'max_step={self.config["max_step"]}',
                f'amr.n_cell={self.config["n_cell"]}',
                f'amr.max_level={self.config["max_level"]}',
                f'amr.plot_file={outdir}/plt',
                f'amr.plot_int={self.config["plot_int"]}',
            ])
            Exec(inner, MpiExecInfo(
                nprocs=nprocs,
                container=self._container_engine,
                container_image=self.deploy_image_name,
                private_dir=self.private_dir,
                gpu=True,
                env=self.mod_env,
            )).run()
        else:
            cmd = [
                'nyx_HydroTests',
                f'max_step={self.config["max_step"]}',
                f'amr.n_cell={self.config["n_cell"]}',
                f'amr.max_level={self.config["max_level"]}',
                f'amr.plot_file={self.config["out"]}/plt',
                f'amr.plot_int={self.config["plot_int"]}',
            ]
            Exec(' '.join(cmd),
                 MpiExecInfo(nprocs=self.config['nprocs'],
                             ppn=self.config['ppn'],
                             hostfile=self.hostfile,
                             env=self.mod_env)).run()

    def stop(self):
        """Stop Nyx (no-op — Nyx runs to completion)."""
        pass

    def clean(self):
        """Remove Nyx output directory."""
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

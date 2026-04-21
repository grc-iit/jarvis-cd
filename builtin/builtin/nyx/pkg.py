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
            {
                'name': 'use_gpu',
                'msg': 'Build with CUDA/GPU backend (Nyx_GPU_BACKEND=CUDA)',
                'type': bool,
                'default': False,
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        use_gpu = self.config.get('use_gpu', False) or 'sci-hpc' in base
        cuda_arch = self.config.get('cuda_arch', 80)
        if use_gpu:
            gpu_flags = (
                f'-DNyx_GPU_BACKEND=CUDA '
                f'"-DAMReX_CUDA_ARCH={cuda_arch}" '
                f'-DCMAKE_CUDA_HOST_COMPILER="$(which g++)" '
            )
            hdf5_flags = '-DAMReX_HDF5=YES -DHDF5_ROOT=/usr/local '
            suffix = f'cuda-{cuda_arch}'
        else:
            gpu_flags = '-DNyx_GPU_BACKEND=NONE '
            hdf5_flags = ''
            suffix = 'cpu'
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': base,
            'HDF5_FLAGS': hdf5_flags,
            'GPU_FLAGS': gpu_flags,
        })
        return content, suffix

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        use_gpu = 'sci-hpc' in base
        deploy_base = ('nvidia/cuda:12.6.0-runtime-ubuntu24.04'
                       if use_gpu else 'ubuntu:24.04')
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': deploy_base,
        })
        return content, suffix

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

        Branches on deploy_mode: uses MpiExecInfo with container engine for
        container mode, MpiExecInfo with hostfile for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            outdir = self.config.get('out', '/tmp/nyx_out')
            Mkdir(outdir).run()

            nprocs = self.config.get('nprocs', 4)
            inputs_file = self.config.get(
                'inputs_file',
                '/opt/Nyx/Exec/HydroTests/inputs.regtest.sedov',
            )
            inner = ' '.join([
                'env',
                'LD_LIBRARY_PATH=/.singularity.d/libs:/usr/local/cuda/lib64:/opt/hdf5/install/lib:/opt/nyx/install/lib',
                '/opt/Nyx/build/Exec/HydroTests/nyx_HydroTests',
                inputs_file,
                f'max_step={self.config["max_step"]}',
                f'amr.n_cell={self.config["n_cell"]}',
                f'amr.max_level={self.config["max_level"]}',
                f'amr.plot_file={outdir}/plt',
                f'amr.plot_int={self.config["plot_int"]}',
            ])
            Exec(inner, MpiExecInfo(
                nprocs=nprocs,
                ppn=self.config.get('ppn'),
                hostfile=self.hostfile,
                port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
                gpu=self.config.get('use_gpu', False),
            )).run()
        else:
            inputs_file = self.config.get(
                'inputs_file',
                '/opt/Nyx/Exec/HydroTests/inputs.regtest.sedov',
            )
            cmd = [
                'nyx_HydroTests',
                inputs_file,
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

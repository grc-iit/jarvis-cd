"""
This module provides classes and methods to launch the LAMMPS application.
LAMMPS (Large-scale Atomic/Molecular Massively Parallel Simulator) is a
classical molecular-dynamics code from Sandia National Laboratories.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Lammps(Application):
    """
    Merged LAMMPS class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run LAMMPS inside a Docker/Podman/Apptainer
    container with Kokkos CUDA.  Set deploy_mode='default' to use a
    system-installed lmp binary via MPI.
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
                'name': 'script',
                'msg': 'Path to LAMMPS input script (e.g., in.lj)',
                'type': str,
                'default': None,
            },
            {
                'name': 'lmp_bin',
                'msg': 'Path to LAMMPS binary (default: lmp in PATH)',
                'type': str,
                'default': 'lmp',
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
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/lammps_out',
            },
            {
                'name': 'kokkos_gpu',
                'msg': 'Enable Kokkos GPU (CUDA) acceleration',
                'type': bool,
                'default': True,
            },
            {
                'name': 'num_gpus',
                'msg': 'Number of GPUs per node',
                'type': int,
                'default': 1,
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        use_gpu = self.config.get('kokkos_gpu', True)
        cuda_arch = self.config.get('cuda_arch', 80)
        if use_gpu:
            cmake_extra = (
                f'-DPKG_KOKKOS=ON '
                f'-DKokkos_ENABLE_CUDA=ON '
                f'"-DKokkos_ARCH_AMPERE{cuda_arch}=ON" '
            )
            suffix = f'kokkos-gpu-{cuda_arch}'
        else:
            cmake_extra = ''
            suffix = 'cpu'
        content = self._read_dockerfile('Dockerfile.build', {
            'BASE_IMAGE': base,
            'CMAKE_EXTRA': cmake_extra,
        })
        return content, suffix

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'nvidia/cuda:12.6.0-runtime-ubuntu24.04',
        })
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure LAMMPS.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            if self.config['out']:
                Mkdir(self.config['out'],
                      PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch LAMMPS.

        Branches on deploy_mode: uses MpiExecInfo with container engine for
        container mode, MpiExecInfo with hostfile for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            cmd = ['/usr/local/bin/lmp']
            if self.config.get('script'):
                cmd.append(f"-in {self.config['script']}")
            if self.config.get('kokkos_gpu'):
                n_gpus = self.config.get('num_gpus', 1)
                cmd += [f'-k on g {n_gpus}', '-sf kk', '-pk kokkos cuda/aware on']

            Exec(' '.join(cmd), MpiExecInfo(
                nprocs=self.config['nprocs'],
                ppn=self.config['ppn'],
                hostfile=self.hostfile,
                port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                gpu=self.config.get('kokkos_gpu', False),
                env=self.mod_env,
            )).run()
        else:
            cmd = [self.config['lmp_bin']]
            if self.config['script']:
                cmd.append(f"-in {self.config['script']}")
            if self.config.get('kokkos_gpu'):
                n_gpus = self.config.get('num_gpus', 1)
                cmd += [f'-k on g {n_gpus}', '-sf kk', '-pk kokkos cuda/aware on']

            Exec(' '.join(cmd),
                 MpiExecInfo(nprocs=self.config['nprocs'],
                             ppn=self.config['ppn'],
                             hostfile=self.hostfile,
                             env=self.mod_env,
                             cwd=self.config.get('out'))).run()

    def stop(self):
        """Stop LAMMPS (no-op — LAMMPS runs to completion)."""
        pass

    def clean(self):
        """Remove LAMMPS output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

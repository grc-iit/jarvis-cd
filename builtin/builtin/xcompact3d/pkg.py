"""
This module provides classes and methods to launch the Xcompact3d application.
Xcompact3d (Incompact3d) is a high-order finite-difference flow solver for
DNS and LES of incompressible turbulent flows.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, LocalExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Xcompact3d(Application):
    """
    Xcompact3d container package supporting both default (bare-metal) and
    container deployment.

    Set deploy_mode='container' to build and run Xcompact3d inside a
    Docker/Podman/Apptainer container with ADIOS2 and 2DECOMP&FFT.
    Set deploy_mode='default' to use a system-installed xcompact3d via MPI.
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
                'name': 'case',
                'msg': 'Xcompact3d test case (e.g., TGV, Channel-Flow)',
                'type': str,
                'default': 'TGV',
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/xcompact3d_out',
            },
            {
                'name': 'base_image',
                'msg': 'Base Docker image for build container',
                'type': str,
                'default': 'ubuntu:24.04',
            },
        ]

    # ------------------------------------------------------------------
    # Container Dockerfile generators
    # ------------------------------------------------------------------

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'ubuntu:24.04')
        content = self._read_dockerfile('Dockerfile.build', {
            'BASE_IMAGE': base,
        })
        return content, 'adios2'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'adios2'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure Xcompact3d.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory on all nodes.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            if self.config['out']:
                Mkdir(self.config['out'],
                      PsshExecInfo(hostfile=self.hostfile,
                                   env=self.env)).run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch Xcompact3d.

        In container mode, runs xcompact3d via MPI inside the deploy
        container. In default mode, runs xcompact3d from PATH.
        """
        if self.config.get('deploy_mode') == 'container':
            cmd = 'xcompact3d'

            Exec(cmd, MpiExecInfo(
                nprocs=self.config['nprocs'],
                ppn=self.config['ppn'],
                hostfile=self.hostfile,
                port=self.ssh_port,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()
        else:
            cmd = 'xcompact3d'
            Exec(cmd, MpiExecInfo(
                nprocs=self.config['nprocs'],
                ppn=self.config['ppn'],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=self.config.get('out'),
            )).run()

    def stop(self):
        """Stop Xcompact3d (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove Xcompact3d output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

"""
This module provides classes and methods to launch the Montage application.
Montage is an astronomical image mosaic engine that assembles FITS images
into custom mosaics. It is developed by the NASA/IPAC Infrared Science
Archive at Caltech.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, LocalExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Montage(Application):
    """
    Montage container package supporting both default (bare-metal) and
    container deployment.

    Set deploy_mode='container' to build and run Montage inside a
    Docker/Podman/Apptainer container.
    Set deploy_mode='default' to use system-installed Montage binaries.
    """

    def _init(self):
        pass

    def _configure_menu(self):
        return [
            {
                'name': 'nprocs',
                'msg': 'Number of MPI processes',
                'type': int,
                'default': 1,
            },
            {
                'name': 'ppn',
                'msg': 'Processes per node',
                'type': int,
                'default': 1,
            },
            {
                'name': 'region',
                'msg': 'Target region name (e.g., M17, M31)',
                'type': str,
                'default': 'M17',
            },
            {
                'name': 'band',
                'msg': '2MASS band (j, h, k)',
                'type': str,
                'default': 'j',
            },
            {
                'name': 'out',
                'msg': 'Output directory for mosaic results',
                'type': str,
                'default': '/tmp/montage_out',
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

    def _build_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        base = self.config.get('base_image', 'sci-hpc-base')
        content = self._read_dockerfile('Dockerfile.build', {
            'BASE_IMAGE': base,
        })
        return content, 'default'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'default'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure Montage.

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
        Launch Montage.

        In container mode, runs /opt/run_mosaic.sh via LocalExecInfo inside
        the deploy container. In default mode, runs the mosaic script locally.
        """
        if self.config.get('deploy_mode') == 'container':
            cmd = '/opt/run_mosaic.sh'

            Exec(cmd, LocalExecInfo(
                env=self.mod_env,
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
            )).run()
        else:
            cmd = '/opt/run_mosaic.sh'
            Exec(cmd, LocalExecInfo(
                env=self.mod_env,
                cwd=self.config.get('out'),
            )).run()

    def stop(self):
        """Stop Montage (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove Montage output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

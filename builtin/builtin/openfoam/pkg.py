"""
This module provides classes and methods to launch OpenFOAM-dev simulations.
OpenFOAM (Open Field Operation And Manipulation) is an open-source CFD
framework from the OpenFOAM Foundation.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Openfoam(Application):
    """
    OpenFOAM container package supporting both default (bare-metal) and
    container deployment.

    Set deploy_mode='container' to build and run OpenFOAM-dev inside a
    Docker/Podman/Apptainer container with ADIOS2 and system OpenMPI.
    Set deploy_mode='default' to use a system-installed OpenFOAM via MPI.

    ``start`` runs ``./Allrun`` (or a user-specified script) in the case
    directory under MPI, matching the tutorial case convention.
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
                'default': 4,
            },
            {
                'name': 'script_location',
                'msg': 'Case directory containing Allrun script',
                'type': str,
                'default': None,
            },
            {
                'name': 'script',
                'msg': 'Script to execute inside script_location',
                'type': str,
                'default': './Allrun',
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
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
        })
        return content, 'openfoam-dev'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'openfoam-dev'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure OpenFOAM.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.
        """
        super()._configure(**kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch OpenFOAM.

        In container mode, runs the case's Allrun script via MPI inside the
        deploy container. In default mode, runs from PATH.
        """
        script = self.config.get('script', './Allrun')
        cwd = self.config.get('script_location')
        foam_env = 'source /opt/OpenFOAM/OpenFOAM-dev/etc/bashrc'
        cmd = f'bash -c "{foam_env} && {script}"'

        if self.config.get('deploy_mode') == 'container':
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
                cwd=cwd,
            )).run()
        else:
            Exec(cmd, MpiExecInfo(
                nprocs=self.config['nprocs'],
                ppn=self.config['ppn'],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=cwd,
            )).run()

    def stop(self):
        """Stop OpenFOAM (no-op — runs to completion)."""
        pass

    def clean(self):
        """OpenFOAM leaves case output in script_location; no global cleanup."""
        pass

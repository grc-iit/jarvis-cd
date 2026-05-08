"""
This module provides classes and methods to launch the ParaView visualization
application for Gray-Scott reaction-diffusion analysis.
ParaView is built with ADIOS2, Catalyst, and Fides for in-situ and
post-hoc parallel visualization of scientific data.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, LocalExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class GrayScottParaview(Application):
    """
    Gray-Scott ParaView container package supporting both default (bare-metal)
    and container deployment.

    Set deploy_mode='container' to build and run ParaView inside a
    Docker/Podman/Apptainer container with ADIOS2 and Catalyst.
    Set deploy_mode='default' to use a system-installed pvbatch/pvserver via MPI.
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
                'msg': 'Path to ParaView Python script (for pvbatch)',
                'type': str,
                'default': None,
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/paraview_out',
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
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': base,
        })
        return content, 'pv513'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'nvidia/cuda:12.6.0-runtime-ubuntu24.04',
        })
        return content, 'pv513'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure ParaView.

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
        Launch ParaView.

        In container mode, runs pvbatch (if a script is provided) or pvserver
        via MPI inside the deploy container. In default mode, runs from PATH.
        """
        if self.config.get('deploy_mode') == 'container':
            script = self.config.get('script')
            if script:
                cmd = f'pvbatch {script}'
            else:
                cmd = 'pvserver'

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
            script = self.config.get('script')
            if script:
                cmd = f'pvbatch {script}'
            else:
                cmd = 'pvserver'

            Exec(cmd, MpiExecInfo(
                nprocs=self.config['nprocs'],
                ppn=self.config['ppn'],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=self.config.get('out'),
            )).run()

    def stop(self):
        """Stop ParaView (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove ParaView output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

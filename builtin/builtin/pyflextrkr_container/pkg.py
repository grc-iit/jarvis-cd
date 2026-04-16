"""
This module provides classes and methods to launch the PyFLEXTRKR
application in a container.  PyFLEXTRKR is a Python FLEXible object
TRacKeR for atmospheric feature tracking (mesoscale convective systems,
convective cells, etc.).

This is a container-capable variant; the bare-metal package lives in
builtin/pyflextrkr/.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class PyflextrkrContainer(Application):
    """
    PyflextrkrContainer class supporting both default (bare-metal) and
    container deployment.

    Set deploy_mode='container' to build and run PyFLEXTRKR inside a
    Docker/Podman/Apptainer container.  Set deploy_mode='default' to use a
    system-installed environment.
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
                'name': 'demo',
                'msg': 'Demo to run',
                'type': str,
                'default': 'mcs_tbpf',
                'choices': ['mcs_tbpf', 'mcs_tbpf_multinode'],
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/pyflextrkr_out',
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
        content = self._read_dockerfile('Dockerfile.build', {
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
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
        Configure PyFLEXTRKR.

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
        Launch PyFLEXTRKR.

        Branches on deploy_mode and demo selection.  The multinode demo
        uses MpiExecInfo; the single-node demo uses LocalExecInfo.
        """
        demo = self.config.get('demo', 'mcs_tbpf')

        if self.config.get('deploy_mode') == 'container':
            Mkdir(self.config['out']).run()

            if demo == 'mcs_tbpf_multinode':
                cmd = '/opt/run_demo_multinode.sh'
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
                cmd = '/opt/run_demo.sh'
                Exec(cmd, LocalExecInfo(
                    container=self._container_engine,
                    container_image=self.deploy_image_name(),
                    shared_dir=self.shared_dir,
                    private_dir=self.private_dir,
                    env=self.mod_env,
                )).run()
        else:
            if demo == 'mcs_tbpf_multinode':
                cmd = '/opt/run_demo_multinode.sh'
                Exec(cmd, MpiExecInfo(
                    nprocs=self.config['nprocs'],
                    ppn=self.config['ppn'],
                    hostfile=self.hostfile,
                    env=self.mod_env,
                )).run()
            else:
                cmd = '/opt/run_demo.sh'
                Exec(cmd, LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        """Stop PyFLEXTRKR (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove PyFLEXTRKR output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

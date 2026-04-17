"""
This module provides classes and methods to launch the WRF application.
WRF (Weather Research and Forecasting) is a mesoscale numerical weather
prediction system designed for both atmospheric research and operational
forecasting applications.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo, LocalExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class WrfContainer(Application):
    """
    WRF container package supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run WRF inside a Docker/Podman/
    Apptainer container with HDF5, NetCDF, ADIOS2, and WPS.
    Set deploy_mode='default' to use a system-installed wrf.exe via MPI.
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
                'msg': 'WRF test case (e.g., em_quarter_ss, em_real)',
                'type': str,
                'default': 'em_quarter_ss',
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/wrf_out',
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
        return content, 'v460'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'v460'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure WRF.

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
        Launch WRF.

        In container mode, runs ideal.exe or wrf.exe via MPI inside the
        deploy container. In default mode, runs wrf.exe from PATH.
        """
        if self.config.get('deploy_mode') == 'container':
            case = self.config.get('case', 'em_quarter_ss')
            if case.startswith('em_') and case != 'em_real':
                # Ideal cases: run ideal.exe first, then wrf.exe
                cmd = f'cd /opt/WRF/test/{case} && ideal.exe && wrf.exe'
            else:
                cmd = 'wrf.exe'

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
            cmd = 'wrf.exe'
            Exec(cmd, MpiExecInfo(
                nprocs=self.config['nprocs'],
                ppn=self.config['ppn'],
                hostfile=self.hostfile,
                env=self.mod_env,
                cwd=self.config.get('out'),
            )).run()

    def stop(self):
        """Stop WRF (no-op -- WRF runs to completion)."""
        pass

    def clean(self):
        """Remove WRF output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

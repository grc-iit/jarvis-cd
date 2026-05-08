"""
This module provides classes and methods to launch the metaGEM application.
metaGEM is a Snakemake workflow for genome-scale metabolic reconstruction
from metagenomic sequencing data.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class Metagem(Application):
    """
    Metagem class supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run metaGEM inside a
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
                'name': 'cores',
                'msg': 'Number of Snakemake cores',
                'type': int,
                'default': 4,
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/metagem_out',
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
        return content, 'snakemake'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'snakemake'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure metaGEM.

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
        Launch metaGEM.

        run_metagem.sh takes the working directory as its first positional
        argument and reads CORES from the environment. We pass self.config['out']
        so Snakemake's root/scratch land under the pipeline's output directory.
        """
        self.mod_env['CORES'] = str(self.config.get('cores', 4))
        cmd = f'/opt/run_metagem.sh {self.config["out"]}'

        if self.config.get('deploy_mode') == 'container':
            Exec(cmd, LocalExecInfo(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()
        else:
            Exec(cmd, LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        """Stop metaGEM (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove metaGEM output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

"""
This module provides classes and methods to launch the rna_seq_star_deseq2
application.  Snakemake workflow for RNA-seq differential expression analysis
with STAR aligner and DESeq2.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class RnaSeqStarDeseq2(Application):
    """
    RnaSeqStarDeseq2 class supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run the RNA-seq pipeline inside a
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
                'default': '/tmp/rnaseq_out',
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
        Configure rna_seq_star_deseq2.

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
        Launch rna_seq_star_deseq2.

        Branches on deploy_mode: uses LocalExecInfo with container engine for
        container mode, LocalExecInfo for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            Mkdir(self.config['out']).run()

            cmd = '/opt/run_rnaseq.sh'
            Exec(cmd, LocalExecInfo(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()
        else:
            cmd = '/opt/run_rnaseq.sh'
            Exec(cmd, LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        """Stop rna_seq_star_deseq2 (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove rna_seq_star_deseq2 output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

"""
This module provides classes and methods to launch the biobb_wf_md_setup
application.  BioExcel Building Blocks MD setup pipeline: fetch PDB,
fix side chains, topology, box, solvation via GROMACS.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class BiobbWfMdSetup(Application):
    """
    BiobbWfMdSetup class supporting both default (bare-metal) and container
    deployment.

    Set deploy_mode='container' to build and run the biobb MD setup pipeline
    inside a Docker/Podman/Apptainer container with GROMACS 2026.
    Set deploy_mode='default' to use a system-installed environment.
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
                'name': 'pdb_file',
                'msg': 'Path to input PDB file',
                'type': str,
                'default': '/opt/biobb-bench/1AKI.pdb',
            },
            {
                'name': 'out',
                'msg': 'Output directory for results',
                'type': str,
                'default': '/tmp/biobb_out',
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
        return content, 'gromacs2026'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'DEPLOY_BASE': 'ubuntu:24.04',
        })
        return content, 'gromacs2026'

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure biobb_wf_md_setup.

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
        Launch biobb_wf_md_setup.

        Container mode delegates to /opt/run_batch.sh, which sets PATH,
        runs the MD setup pipeline in a /tmp scratch dir (GROMACS/biobb
        use atomic write-then-rename, which wrp_cte_fuse does not
        support), and stages the finished outputs into the configured
        output directory via plain cp — which exercises the CTE adapter
        when ``out`` is on the FUSE mountpoint.

        run_md_setup.py takes POSITIONAL args (pdb, workdir), not
        --pdb/--out flags; the wrapper script handles that and lets a
        pdb_file config point at either a single PDB or a directory of
        PDBs.
        """
        if self.config.get('deploy_mode') == 'container':
            cmd = (
                f"/opt/run_batch.sh '{self.config['pdb_file']}' "
                f"'{self.config['out']}'"
            )
            Exec(cmd, LocalExecInfo(
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                env=self.mod_env,
            )).run()
        else:
            cmd = ' '.join([
                'python3', '/opt/run_md_setup.py',
                self.config['pdb_file'],
                self.config['out'],
            ])
            Exec(cmd, LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        """Stop biobb_wf_md_setup (no-op -- runs to completion)."""
        pass

    def clean(self):
        """Remove biobb_wf_md_setup output directory."""
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

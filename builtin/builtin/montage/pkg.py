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
                'name': 'size',
                'msg': ('Region size in degrees (square box centered on '
                        'the region). 0.2° produces tens of MB; 1.0° a '
                        'few GB; 2.0° tens of GB. Triggers runtime fetch '
                        'from 2MASS/IRSA when != 0.2 (the default that '
                        'the SIF pre-stages at build time).'),
                'type': float,
                'default': 0.2,
            },
            {
                'name': 'scratch_dir',
                'msg': ('Scratch dir inside the container for Montage '
                        'intermediates (projected/diffs/corrected). '
                        'Default ~/montage-scratch — keep off /tmp '
                        'because intermediates can be tens of GB.'),
                'type': str,
                'default': '${HOME}/montage-scratch',
            },
            {
                'name': 'out',
                'msg': 'Output directory for mosaic results',
                'type': str,
                'default': '${HOME}/montage_out',
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
        # Inline run_mosaic.sh into build.sh as base64 so the container
        # build is self-contained. jarvis's aux-file copy step in
        # pipeline.py runs `docker cp` with hide_output=True and does not
        # check exit codes, so a silently-failed copy manifests here as
        # "cp: cannot stat 'run_mosaic.sh'" deep in the build.
        import base64
        import os
        run_mosaic_path = os.path.join(self.pkg_dir, 'run_mosaic.sh')
        with open(run_mosaic_path, 'rb') as f:
            run_mosaic_b64 = base64.b64encode(f.read()).decode('ascii')
        content = self._read_build_script('build.sh', {
            'BASE_IMAGE': base,
            'RUN_MOSAIC_B64': run_mosaic_b64,
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
        Always propagates MONTAGE_* env vars so the run_mosaic.sh inside
        the SIF picks up region/band/size/scratch_dir/out overrides
        without rebuilding the image.
        """
        super()._configure(**kwargs)

        # Propagate config knobs to run_mosaic.sh via env vars (it reads
        # MONTAGE_REGION / MONTAGE_BAND / MONTAGE_SIZE / MONTAGE_OUT /
        # MONTAGE_SCRATCH_DIR; see updated run_mosaic.sh).
        self.setenv('MONTAGE_REGION', str(self.config.get('region', 'M17')))
        self.setenv('MONTAGE_BAND', str(self.config.get('band', 'j')).upper())
        self.setenv('MONTAGE_SIZE', str(self.config.get('size', 0.2)))
        if self.config.get('out'):
            self.setenv('MONTAGE_OUT', str(self.config['out']))
        if self.config.get('scratch_dir'):
            self.setenv('MONTAGE_SCRATCH_DIR', str(self.config['scratch_dir']))

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

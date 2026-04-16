"""
This module provides classes and methods to launch the Gray-Scott application.
Gray-Scott is a reaction-diffusion simulation.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
from jarvis_cd.util.config_parser import JsonFile
from jarvis_cd.util.logger import Color
import time


class GrayScott(Application):
    """
    Merged Gray-Scott class supporting both default (bare-metal) and container deployment.

    Set deploy_mode='container' to build and run Gray-Scott inside a Docker/Podman/Apptainer
    container with CUDA+MPI+HDF5.  Set deploy_mode='default' to use a system-installed
    gray-scott binary via MPI with ADIOS2 I/O.
    """

    def _init(self):
        self.adios2_xml_path = f'{self.shared_dir}/adios2.xml'
        self.settings_json_path = f'{self.shared_dir}/settings-files.json'

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
                'default': None,
            },
            {
                'name': 'width',
                'msg': 'Global grid width (columns)',
                'type': int,
                'default': 512,
            },
            {
                'name': 'height',
                'msg': 'Global grid height (rows)',
                'type': int,
                'default': 512,
            },
            {
                'name': 'steps',
                'msg': 'Total number of time steps',
                'type': int,
                'default': 5000,
            },
            {
                'name': 'out_every',
                'msg': 'HDF5 output interval (steps between writes)',
                'type': int,
                'default': 500,
            },
            {
                'name': 'outdir',
                'msg': 'Output directory for HDF5 files',
                'type': str,
                'default': '/tmp/gray_scott_out',
            },
            {
                'name': 'F',
                'msg': 'Feed rate',
                'type': float,
                'default': 0.035,
            },
            {
                'name': 'k',
                'msg': 'Kill rate',
                'type': float,
                'default': 0.060,
            },
            {
                'name': 'Du',
                'msg': 'Diffusion coefficient for u',
                'type': float,
                'default': 0.16,
            },
            {
                'name': 'Dv',
                'msg': 'Diffusion coefficient for v',
                'type': float,
                'default': 0.08,
            },
            {
                'name': 'cuda_arch',
                'msg': 'CUDA architecture code (80=A100, 90=H100, 70=V100)',
                'type': int,
                'default': 80,
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
        cuda_arch = self.config.get('cuda_arch', 80)
        content = self._read_dockerfile('Dockerfile.build', {
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
            'CUDA_ARCH': cuda_arch,
        })
        return content, f'cuda-{cuda_arch}'

    def _build_deploy_phase(self):
        if self.config.get('deploy_mode') != 'container':
            return None
        suffix = getattr(self, '_build_suffix', '')
        content = self._read_dockerfile('Dockerfile.deploy', {
            'BUILD_IMAGE': self.build_image_name(),
            'BASE_IMAGE': self.config.get('base_image', 'sci-hpc-base'),
        })
        return content, suffix

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, **kwargs):
        """
        Configure Gray-Scott.

        Calls super()._configure() which updates self.config and (when
        deploy_mode == 'container') triggers build_phase / build_deploy_phase.

        In default mode, also creates the output directory, writes the
        ADIOS2 XML config, and saves the settings JSON file.
        """
        super()._configure(**kwargs)

        if self.config.get('deploy_mode') == 'default':
            output = self.config.get('outdir', f'{self.shared_dir}/gray-scott-output')
            self.config['outdir'] = output
            Mkdir(output, PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

            settings_json = {
                'L': self.config.get('width', 512),
                'Du': self.config.get('Du', 0.16),
                'Dv': self.config.get('Dv', 0.08),
                'F': self.config.get('F', 0.035),
                'k': self.config.get('k', 0.060),
                'dt': 2.0,
                'plotgap': self.config.get('out_every', 500),
                'steps': self.config.get('steps', 5000),
                'noise': 0.01,
                'output': output,
                'adios_config': self.adios2_xml_path
            }
            JsonFile(self.settings_json_path).save(settings_json)
            self.copy_template_file(f'{self.pkg_dir}/config/adios2.xml',
                                    self.adios2_xml_path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Launch Gray-Scott.

        Branches on deploy_mode: uses MpiExecInfo with container engine for
        container mode, MpiExecInfo with hostfile and ADIOS2 settings JSON for default mode.
        """
        if self.config.get('deploy_mode') == 'container':
            outdir = self.config.get('outdir', '/tmp/gray_scott_out')
            Mkdir(outdir).run()

            nprocs = self.config.get('nprocs', 4)
            inner = ' '.join([
                '/usr/bin/gray_scott',
                f'--width {self.config["width"]}',
                f'--height {self.config["height"]}',
                f'--steps {self.config["steps"]}',
                f'--out-every {self.config["out_every"]}',
                f'--outdir {outdir}',
                f'--F {self.config["F"]}',
                f'--k {self.config["k"]}',
                f'--Du {self.config["Du"]}',
                f'--Dv {self.config["Dv"]}',
            ])
            Exec(inner, MpiExecInfo(
                nprocs=nprocs,
                ppn=self.config.get('ppn'),
                container=self._container_engine,
                container_image=self.deploy_image_name(),
                shared_dir=self.shared_dir,
                private_dir=self.private_dir,
                gpu=True,
                env=self.mod_env,
            )).run()
        else:
            start = time.time()
            Exec(f'gray-scott {self.settings_json_path}',
                 MpiExecInfo(nprocs=self.config['nprocs'],
                             ppn=self.config['ppn'],
                             hostfile=self.hostfile,
                             env=self.mod_env)).run()
            end = time.time()
            self.log(f'TIME: {end - start:.2f} seconds', color=Color.GREEN)

    def stop(self):
        """Stop Gray-Scott (no-op — Gray-Scott runs to completion)."""
        pass

    def clean(self):
        """Remove Gray-Scott output directory."""
        output = self.config.get('outdir', '')
        if output:
            Rm(output + '*').run()

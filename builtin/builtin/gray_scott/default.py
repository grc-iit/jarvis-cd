"""
Default (bare-metal) Gray-Scott deployment using the ADIOS2 benchmark version.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
from jarvis_cd.util.config_parser import JsonFile
from jarvis_cd.util.logger import Color
import time
import os


class GrayScottDefault(Application):
    """
    Default Gray-Scott deployment using system-installed gray-scott binary.
    This version uses ADIOS2 for I/O and a JSON settings file.
    """

    def _init(self):
        self.adios2_xml_path = f'{self.shared_dir}/adios2.xml'
        self.settings_json_path = f'{self.shared_dir}/settings-files.json'

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
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
        self.copy_template_file(f'{self.pkg_dir}/config/adios2.xml', self.adios2_xml_path)

    def start(self):
        start = time.time()
        Exec(f'gray-scott {self.settings_json_path}',
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.hostfile,
                         env=self.mod_env)).run()
        end = time.time()
        self.log(f'TIME: {end - start:.2f} seconds', color=Color.GREEN)

    def stop(self):
        pass

    def clean(self):
        output = self.config.get('outdir', '')
        if output:
            Rm(output + '*').run()

"""
Default (bare-metal) WarpX deployment.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
import os


class WarpxDefault(Application):
    """Default WarpX deployment using system-installed warpx binary."""

    def _init(self):
        self.warpx_bin = None

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
        Mkdir(self.config['out'],
              PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()
        # Find warpx binary (various naming conventions)
        import shutil
        for name in ['warpx.3d.MPI.CUDA.SP', 'warpx', 'warpx.3d']:
            if shutil.which(name):
                self.warpx_bin = name
                break
        if not self.warpx_bin:
            self.warpx_bin = 'warpx.3d.MPI.CUDA.SP'

    def start(self):
        if self.config['inputs']:
            inputs_arg = self.config['inputs']
            cwd = os.path.dirname(self.config['inputs'])
        elif self.config['example'] != 'custom':
            example_dir = f'/opt/warpx/Examples/Physics_applications/{self.config["example"]}'
            inputs_arg = f'inputs_base_3d'
            cwd = example_dir
        else:
            raise ValueError("Either 'inputs' or 'example' must be specified")

        cmd = [
            self.warpx_bin,
            inputs_arg,
            f'max_step={self.config["max_step"]}',
            f'amr.n_cell={self.config["n_cell"]}',
            f'amr.plot_file={self.config["out"]}/plt',
            f'amr.plot_int={self.config["plot_int"]}',
        ]

        Exec(' '.join(cmd),
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.hostfile,
                         env=self.mod_env,
                         cwd=cwd)).run()

    def stop(self):
        pass

    def clean(self):
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

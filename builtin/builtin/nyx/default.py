"""
Default (bare-metal) Nyx HydroTests deployment.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class NyxDefault(Application):
    """Default Nyx deployment using system-installed nyx_HydroTests binary."""

    def _init(self):
        pass

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
        Mkdir(self.config['out'],
              PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    def start(self):
        cmd = [
            'nyx_HydroTests',
            f'max_step={self.config["max_step"]}',
            f'amr.n_cell={self.config["n_cell"]}',
            f'amr.max_level={self.config["max_level"]}',
            f'amr.plot_file={self.config["out"]}/plt',
            f'amr.plot_int={self.config["plot_int"]}',
        ]

        Exec(' '.join(cmd),
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.hostfile,
                         env=self.mod_env)).run()

    def stop(self):
        pass

    def clean(self):
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

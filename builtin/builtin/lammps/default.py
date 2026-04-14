"""
Default (bare-metal) LAMMPS deployment.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
import os


class LammpsDefault(Application):
    """
    Default LAMMPS deployment using system-installed lmp binary.
    """

    def _init(self):
        pass

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
        if self.config['out']:
            Mkdir(self.config['out'],
                  PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    def start(self):
        cmd = [self.config['lmp_bin']]
        if self.config['script']:
            cmd.append(f"-in {self.config['script']}")
        if self.config.get('kokkos_gpu'):
            n_gpus = self.config.get('num_gpus', 1)
            cmd += [f'-k on g {n_gpus}', '-sf kk', '-pk kokkos cuda/aware on']

        Exec(' '.join(cmd),
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.hostfile,
                         env=self.mod_env,
                         cwd=self.config.get('out'))).run()

    def stop(self):
        pass

    def clean(self):
        if self.config['out']:
            Rm(self.config['out'] + '*',
               PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

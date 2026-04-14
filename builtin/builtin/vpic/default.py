"""
Default (bare-metal) VPIC deployment.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, MpiExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm
import os


class VpicDefault(Application):
    """Default VPIC deployment using system-installed vpic binary."""

    def _init(self):
        self.deck_binary = None

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
        Mkdir(self.config['run_dir'],
              PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    def start(self):
        run_dir = self.config['run_dir']

        if self.config.get('deck'):
            deck_file = self.config['deck']
        else:
            sample = self.config.get('sample_deck', 'harris')
            deck_file = f'/opt/vpic-kokkos/sample/{sample}/{sample}.cxx'

        # Step 1: Compile the deck
        deck_name = os.path.basename(deck_file).replace('.cxx', '')
        Exec(f'cp {deck_file} {run_dir}/ && cd {run_dir} && vpic {deck_name}.cxx',
             MpiExecInfo(nprocs=1, hostfile=self.hostfile, env=self.mod_env,
                         cwd=run_dir)).run()

        # Step 2: Run compiled binary
        binary = f'{run_dir}/{deck_name}.Linux'
        Exec(binary,
             MpiExecInfo(nprocs=self.config['nprocs'],
                         ppn=self.config['ppn'],
                         hostfile=self.hostfile,
                         env=self.mod_env,
                         cwd=run_dir)).run()

    def stop(self):
        pass

    def clean(self):
        Rm(self.config['run_dir'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

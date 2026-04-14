"""
Default (bare-metal) AI Training deployment using torchrun.
"""
from jarvis_cd.core.pkg import Application
from jarvis_cd.shell import Exec, LocalExecInfo, PsshExecInfo
from jarvis_cd.shell.process import Mkdir, Rm


class AiTrainingDefault(Application):
    """Default AI Training using system-installed Python/torchrun."""

    def _init(self):
        pass

    def _configure(self, **kwargs):
        super()._configure(**kwargs)
        Mkdir(self.config['out'],
              PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()

    def start(self):
        node_rank = 0  # Single-node or head node
        cmd = [
            'torchrun',
            f'--nnodes={self.config["nnodes"]}',
            f'--nproc_per_node={self.config["nproc_per_node"]}',
            f'--node_rank={node_rank}',
            f'--master_addr={self.config["master_addr"]}',
            f'--master_port={self.config["master_port"]}',
            self.config['script'],
            f'--epochs {self.config["epochs"]}',
            f'--batch {self.config["batch"]}',
        ]
        Exec(' '.join(cmd), LocalExecInfo(env=self.mod_env)).run()

    def stop(self):
        pass

    def clean(self):
        Rm(self.config['out'] + '*',
           PsshExecInfo(hostfile=self.hostfile, env=self.env)).run()


from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.basic.hostfile import Hostfile
from jarvis_cd.ssh.ssh_info_mixin import SSHInfoMixin
import getpass
import os

class InteractiveSSHNode(ExecNode,SSHInfoMixin):
    def __init__(self, host, only_init=False):
        if isinstance(host, Hostfile):
            host = host.hosts[0]
        #Set default values
        self.host = host
        self.only_init = only_init
        self.cmd = ''
        if self.only_init:
            self.cmd = 'echo'

        self._ProcessSSHInfo()
        ssh_cmd = [
            f"ssh",
            f"-i {self.pkey}" if self.pkey is not None else None,
            f"-p {self.port}" if self.port is not None else None,
            f"{self.username}@{host}" if self.username is not None else host,
            self.cmd
        ]
        ssh_cmd = [tok for tok in ssh_cmd if tok is not None]
        ssh_cmd = " ".join(ssh_cmd)
        super().__init__(ssh_cmd, print_output=True, collect_output=False)
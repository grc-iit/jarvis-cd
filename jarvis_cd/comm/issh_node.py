
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.hostfile import Hostfile
import getpass
import os

class InteractiveSSHNode(ExecNode):
    def __init__(self, host, ssh_info, only_init=False):
        if isinstance(host, Hostfile):
            host = host.hosts[0]
        #Set default values
        self.username = None
        self.pkey = None
        self.password = None
        self.port = None
        self.host = host
        self.only_init = only_init
        self.cmd = ''
        if self.only_init:
            self.cmd = 'echo'

        #Prioritize SSH info struct
        if ssh_info:
            if 'username' in ssh_info:
                self.username = ssh_info['username']
            if 'key' in ssh_info and 'key_dir' in ssh_info:
                self.pkey = os.path.join(ssh_info['key_dir'], ssh_info['key'])
            if 'port' in ssh_info:
                self.port = ssh_info['port']

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
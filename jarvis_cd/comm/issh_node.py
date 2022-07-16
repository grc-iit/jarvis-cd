
from jarvis_cd.shell.exec_node import ExecNode
import getpass
import os


class InteractiveSSHNode(ExecNode):
    def __init__(self, host, ssh_info, only_init=False):
        #Set default values
        self.username = getpass.getuser()
        self.pkey = None
        self.password = None
        self.port = 22
        self.host = host
        self.only_init = only_init
        self.cmd = ''
        if self.only_init:
            self.cmd = 'echo'

        #Prioritize SSH info struct
        if 'username' in ssh_info:
            self.username = ssh_info['username']
        if 'key' in ssh_info and 'key_dir' in ssh_info:
            self.pkey = os.path.join(ssh_info['key_dir'], ssh_info['key'])
        if 'port' in ssh_info:
            self.port = ssh_info['port']

        #Build SSH command
        if self.pkey:
            cmd = f"ssh -i {self.pkey} -p {self.port} {self.username}@{self.host} {self.cmd}"
        else:
            cmd = f"ssh -p {self.port} {self.username}@{self.host} {self.cmd}"
        super().__init__(cmd, print_output=True, collect_output=False, print_fancy=False)
from pssh.pssh_client import ParallelSSHClient
import sys
import os
import getpass

from jarvis_cd.node import Node

sys.stderr = sys.__stderr__

class SSHNode(Node):
    def __init__(self, hosts, cmd, username = getpass.getuser(), sudo=False):
        if type(hosts) == list:
            self.hosts=hosts
        else:
            self.hosts=[hosts]
        if type(cmd) == list:
            self.cmds=cmd
        else:
            self.cmds=[cmd]
        self.sudo=sudo
        self.username=username

    def _exec_ssh(self, cmd):
        client = ParallelSSHClient(self.hosts)
        output = client.run_command(cmd, sudo=self.sudo)
        return output

    def Run(self):
        output = []*len(self.cmds)
        for i,cmd in enumerate(self.cmds):
            output[i] = self._exec_ssh(cmd)
        return output






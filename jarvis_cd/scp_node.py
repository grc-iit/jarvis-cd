from pssh.pssh_client import ParallelSSHClient
from gevent import joinall
import sys
import os
import getpass

from jarvis_cd.node import Node

sys.stderr = sys.__stderr__

class SCPNode(Node):
    def __init__(self, hosts, source, destination, username = getpass.getuser(), sudo=False, print_output=False):
        super().__init__(print_output)
        if type(hosts) == list:
            self.hosts=hosts
        else:
            self.hosts=[hosts]
        self.source = source
        self.destination = destination
        self.sudo=sudo
        self.username=username

    def _exec_scp(self):
        client = ParallelSSHClient(self.hosts)
        output = client.copy_file(self.source, self.destination,True)
        joinall(output, raise_error=True)
        return output

    def Run(self):
        output = self._exec_scp()
        if self.print_output:
            print(output)
        return output






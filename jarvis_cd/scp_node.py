from pssh.clients import ParallelSSHClient
from gevent import joinall
import sys
import os
import getpass
from jarvis_cd.hostfile import Hostfile

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class SCPNode(Node):
    def __init__(self, name, hosts, source, destination, username = getpass.getuser(), port=22, sudo=False, print_output=False):
        super().__init__(name, print_output)
        if isinstance(hosts, list):
            self.hosts = hosts
        elif isinstance(hosts, str):
            self.hosts = [hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SCPNode hosts", type(hosts))


        #There's a bug in SCP which cannot copy a file to itself
        if source == destination:
            self.hosts = [host for host in hosts if host != "localhost" and host != "127.0.0.1"]

        self.source = source
        self.destination = destination
        self.sudo=sudo
        self.username=username
        self.port = port

    def _exec_scp(self):
        if len(self.hosts) == 0:
            return
        client = ParallelSSHClient(self.hosts, port=self.port)
        output = client.copy_file(self.source, self.destination,True)
        joinall(output, raise_error=True)
        nice_output = dict()
        for host in output:
            nice_output[host] = {
                'stdout': [],
                'stderr': []
            }
        return nice_output

    def Run(self):
        output = self._exec_scp()
        if self.print_output:
            self.Print(output)
        return output

    def __str__(self):
        return "SCPNode {}".format(self.name)

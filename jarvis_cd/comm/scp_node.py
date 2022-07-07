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
    def __init__(self, name, hosts, source, destination,
                 username = getpass.getuser(), pkey=None, password=None, port=22,
                 sudo=False, print_output=True, collect_output=True, host_aliases=None):
        super().__init__(name, print_output, collect_output)
        if isinstance(hosts, list):
            self.hosts = hosts
        elif isinstance(hosts, str):
            self.hosts = [hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SCPNode hosts", type(hosts))

        # Do not execute SCP if only localhost
        if self.hosts[0] == 'localhost' and len(self.hosts) == 1:
            self.hosts = []

        #There's a bug in SCP which cannot copy a file to itself
        if source == destination:
            if host_aliases is None:
                print("WARNING!!! If the machine running this command is also in the hostfile, scp will bug out and remove the data.")
            else:
                for alias in host_aliases:
                    self.hosts.remove(alias)

        if password is None and pkey is None:
            pkey = f"{os.environ['HOME']}/.ssh/id_rsa"

        self.source = source
        self.destination = destination
        self.sudo=sudo
        self.username=username
        self.port = int(port)
        self.pkey = pkey
        self.password = password

    def _exec_scp(self):
        if len(self.hosts) == 0:
            return
        client = ParallelSSHClient(self.hosts, user=self.username, pkey=self.pkey, password=self.password, port=self.port)
        output = client.copy_file(self.source, self.destination,True)
        joinall(output, raise_error=True)
        self.output = [{}]
        for host in output:
            self.output[0][host] = {
                'stdout': [],
                'stderr': []
            }
        return self

    def _Run(self):
        self._exec_scp()
        return self

    def __str__(self):
        return "SCPNode {}".format(self.name)

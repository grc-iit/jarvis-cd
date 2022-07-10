from pssh.clients import ParallelSSHClient
from gevent import joinall
import sys, os
import getpass
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.comm.ssh_config import SSHArgs

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class SCPNode(Node,SSHArgs):
    def __init__(self, name, sources, destination,
                 hosts=None, username=None, pkey=None, password=None, port=22,
                 sudo=False, shell=True, host_aliases=None, ssh_info=None,
                 print_output=True, collect_output=True):
        super().__init__(name, print_output, collect_output)
        self._ProcessArgs(hosts=hosts, username=username, pkey=pkey, password=password, port=port,
                 sudo=sudo, shell=shell, host_aliases=host_aliases, ssh_info=ssh_info)

        #Make sure the sources is a list
        if isinstance(sources, list):
            self.sources = sources
        elif isinstance(sources, str):
            self.sources = [sources]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SCPNode source paths", type(sources))

        #Store destination
        self.destination = destination

        #There's a bug in SCP which cannot copy a file to itself
        for source in self.sources:
            src_file = os.path.normpath(source)
            dst_file = os.path.normpath(destination)
            src_dir = os.path.normpath(os.path.dirname(source))
            dst_dir = os.path.normpath(destination)
            if src_file == dst_file or src_dir == dst_dir:
                self.hosts = self.hosts.copy()
                if 'localhost' in self.hosts:
                    self.hosts.remove('localhost')
                for alias in self.host_aliases:
                    if alias in self.hosts:
                        self.hosts.remove(alias)
                break

    def _exec_scp(self):
        client = ParallelSSHClient(self.hosts, user=self.username, pkey=self.pkey, password=self.password,
                                   port=self.port)
        for source in self.sources:
            destination = self.destination
            if len(self.sources) > 1:
                destination = os.path.join(self.destination, os.path.basename(source))
            output = client.copy_file(source, destination, recurse=os.path.isdir(source))
            joinall(output, raise_error=True)

    def _Run(self):
        if self.do_ssh:
            self._exec_scp()
        return self

    def __str__(self):
        return "SCPNode {}".format(self.name)

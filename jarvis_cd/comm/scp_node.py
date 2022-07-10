from pssh.clients import ParallelSSHClient
from gevent import joinall
import sys, os
from jarvis_cd.basic.parallel_node import ParallelNode

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class SCPNode(ParallelNode):
    def __init__(self, sources, destination, **kwargs):
        super().__init__(**kwargs)

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

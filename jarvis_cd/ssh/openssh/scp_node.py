from pssh.clients import ParallelSSHClient
from gevent import joinall
import sys, os
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.fs.mv_node import MvNode
from jarvis_cd.fs.ls_node import LsNode
from jarvis_cd.basic.exception import Error, ErrorCode
from jarvis_cd.basic.echo_node import EchoNode
from jarvis_cd.basic.enumerations import Color
from jarvis_cd.shell.local_exec_node import LocalExecNode

sys.stderr = sys.__stderr__

"""
SCPNode has various concerns:
    1. /home/cc/hi.txt -> /home/cc will result in error, since /home/cc is a directory. Must specify full path.
    2. Pscp (pssh.clients) cannot recursively copy directories if directory already exists on destination. Fixed.
    3. /home/cc/hi.txt -> /home/cc/hi.txt will delete hi.txt if the same host executing SCP is also in the hostfile. Fixed.
"""

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

        #Cannot copy a file to itself
        for source in self.sources:
            src_file = os.path.normpath(source)
            dst_file = os.path.normpath(destination)
            src_dir = os.path.normpath(os.path.dirname(source))
            dst_dir = os.path.normpath(destination)
            if src_file == dst_file or src_dir == dst_dir:
                self.hosts = self.hosts_no_alias
                EchoNode("Copying files to the same location. Ignoring host aliases.", color=Color.YELLOW).Run()
                break

    def _copy_file(self, client, source, destination, recurse=False):
        if recurse:
            RmNode(destination, **self.GetClassParams(ParallelNode)).Run()
        if self.sudo:
            tmp_dst = os.path.join('/tmp', os.path.basename(source))
            if recurse:
                RmNode(tmp_dst, **self.GetClassParams(ParallelNode)).Run()
            output = client.copy_file(source, tmp_dst, recurse=recurse)
            joinall(output, raise_error=True)
            MvNode(tmp_dst, destination, hosts=self.hosts).Run()
        else:
            output = client.copy_file(source, destination, recurse=recurse)
            joinall(output, raise_error=True)

    def _exec_scp(self):
        EchoNode(f"pssh {self.hosts} user={self.username} pkey={self.pkey} port={self.port}", color=Color.YELLOW).Run()
        client = ParallelSSHClient(self.hosts, user=self.username, pkey=self.pkey, password=self.password,
                                   port=self.port)
        #Expand all directories
        dirs = {}
        files = {}
        for source in self.sources:
            dst_path = self.destination
            if os.path.isdir(self.destination):
                dst_path = os.path.join(self.destination, os.path.basename(source))
            if os.path.isdir(source):
                dirs[source] = dst_path
            else:
                files[source] = dst_path

        #Copy all files to the remote host
        for source,destination in files.items():
            self._copy_file(client, source, destination)
        for source,destination in dirs.items():
            self._copy_file(client, source, destination, recurse=True)

    def _Run(self):
        if self.do_ssh:
            self._exec_scp()
        return self

    def __str__(self):
        return "SCPNode {}".format(self.name)

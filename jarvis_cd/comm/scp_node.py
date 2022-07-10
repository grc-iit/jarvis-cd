from pssh.clients import ParallelSSHClient
from gevent import joinall
import sys, os
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.basic.mkdir_node import MkdirNode
from jarvis_cd.basic.rm_node import RmNode
from jarvis_cd.basic.ls_node import LsNode
from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

"""
SCPNode has various concerns:
    1. /home/cc/hi.txt -> /home/cc will result in error, since /home/cc is a directory. Must specify full path.
    2. Pscp cannot recursively copy directories if directory already exists on destination. Fixed.
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

        #There's a bug in SCP which canot copy a file to itself
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

        #Expand all directories
        dirs = {}
        files = {}
        for source in self.sources:
            #source is either a single file or a directory
            is_dir = os.path.isdir(source)
            source_files = [source]

            #Create source dir -> destination command
            if is_dir:
                node = LsNode(source).Run()
                #source_files = node.GetFiles()
                source_files = []
                rel_path = os.path.relpath(source, source)
                dst_path = os.path.join(self.destination, rel_path)
                dirs[source] = dst_path

            #Create source file -> destination command
            for file in source_files:
                #We are copying a directory to another directory. Override if exists
                if is_dir:
                    rel_path = os.path.relpath(file, source)
                    dst_path = os.path.join(self.destination, rel_path)
                #We are copying a set of files into "destination"
                elif len(self.sources) > 1:
                    dst_path = os.path.join(self.destination, os.path.basename(source))
                #We are copying a single file into "destination"
                else:
                    dst_path = self.destination
                files[file] = dst_path

        #Create new remote directories
        #MkdirNode(list(dirs.values()), **self._GetParams(ParallelNode)).Run()
        RmNode(list(dirs.values()), **self._GetParams(ParallelNode)).Run()

        #Copy all files to the remote host
        for source,destination in files.items():
            output = client.copy_file(source, destination)
            joinall(output, raise_error=True)
        for source,destination in dirs.items():
            output = client.copy_file(source, destination, recurse=True)
            joinall(output, raise_error=True)

    def _Run(self):
        if self.do_ssh:
            self._exec_scp()
        return self

    def __str__(self):
        return "SCPNode {}".format(self.name)

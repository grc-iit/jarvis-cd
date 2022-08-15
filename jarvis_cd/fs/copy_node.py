
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.ssh.openssh.scp_node import SCPNode
from jarvis_cd.basic.exception import Error,ErrorCode
import shutil, os


class CopyNode(ParallelNode):
    def __init__(self, sources, destination=None, **kwargs):
        super().__init__(**kwargs)

        if destination is None:
            if isinstance(sources, str):
                destination = sources
            elif isinstance(sources, list) and len(sources) == 1:
                destination = sources[0]

        # Make sure the sources is a list
        if isinstance(sources, list):
            sources = sources
        elif isinstance(sources, str):
            sources = [sources]
        else:
            raise Error(ErrorCode.INVALID_TYPE).format(obj="SCPNode source paths", t=type(sources))

        self.sources = [source for source in sources if source is not None]
        self.destination = destination

    def _Run(self):
        if self.do_ssh:
            node = SCPNode(self.sources, self.destination, **self.GetClassParams(ParallelNode, print_output=False)).Run()
            self.output = node.GetOutput()
        else:
            for source in self.sources:
                src_file = os.path.normpath(source)
                dst_file = os.path.normpath(self.destination)
                src_dir = os.path.normpath(os.path.dirname(source))
                dst_dir = os.path.normpath(self.destination)
                if src_file == dst_file or src_dir == dst_dir:
                    continue
                if os.path.isdir(source):
                    shutil.copytree(source, self.destination)
                else:
                    shutil.copy(source, self.destination)

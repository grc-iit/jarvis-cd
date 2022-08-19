
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
            raise Error(ErrorCode.INVALID_TYPE).format("SCPNode source paths", type(sources))

        self.sources = [source for source in sources if source is not None]
        self.destination = destination

    def _Run(self):
        if self.do_ssh:
            node = SCPNode(self.sources, self.destination, **self.GetClassParams(ParallelNode, print_output=False)).Run()
            self.output = node.GetOutput()
        else:
            cmds = [f"cp -r {source} {self.destination}" for source in self.sources]
            ExecNode(cmds, **self.GetClassParams(ParallelNode, print_output=False)).Run()

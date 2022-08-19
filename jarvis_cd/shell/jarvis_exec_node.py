
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.basic.echo_node import EchoNode
from jarvis_cd.basic.enumerations import Color
from jarvis_cd.serialize.pickle import PickleFile
from jarvis_cd.fs.copy_node import CopyNode
from abc import abstractmethod
import os

class JarvisExecNode(ParallelNode):
    def _Run(self):
        # We ignore ParallelNode since we don't want to call SSH during _LocalRun
        path = f"{hash(str(self))}.jarvis_node"
        cmd = f"jarvis base exec {os.path.join('tmp', path)}"
        PickleFile(path).Save(self)
        CopyNode(path, **self.GetClassParams(ParallelNode, print_output=False, shell=True)).Run()
        node = ExecNode(cmd, **self.GetClassParams(ParallelNode, print_output=False, shell=True)).Run()
        self.output = node.GetOutput()

    @abstractmethod
    def _LocalRun(self):
        return
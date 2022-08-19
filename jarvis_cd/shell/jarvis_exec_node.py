
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.basic.echo_node import EchoNode
from jarvis_cd.basic.enumerations import Color
from abc import abstractmethod

class JarvisExecNode(ParallelNode):
    def _Run(self):
        # We ignore ParallelNode since we don't want to call SSH during _LocalRun
        cmd = self._ToShellCmd(ignore_base=ParallelNode)
        #EchoNode(cmd, color=Color.YELLOW).Run()
        node = ExecNode(cmd, **self.GetClassParams(ParallelNode, print_output=False, shell=True)).Run()
        self.output = node.GetOutput()

    @abstractmethod
    def _LocalRun(self):
        return
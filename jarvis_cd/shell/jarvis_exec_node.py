
from jarvis_cd.parallel_node import ParallelNode
from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.basic.echo_node import EchoNode
from abc import abstractmethod

class JarvisExecNode(ParallelNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _Run(self):
        if self.do_ssh:
            # We ignore kwargs since we don't want to call SSH during _LocalRun
            cmd = self._ToShellCmd(ignore_base=ParallelNode)
            node = ExecNode(cmd, **self.GetClassParams(ParallelNode, print_output=False, shell=True)).Run()
            #EchoNode(cmd).Run()
            self.output = node.GetOutput()
        else:
            self._LocalRun()

    @abstractmethod
    def _LocalRun(self):
        return
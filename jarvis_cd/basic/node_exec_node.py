
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.basic.exec_node import ExecNode
from abc import ABC,abstractmethod

class NodeExecNode(ParallelNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cmd = self._ToShellCmd()
        self.kwargs = kwargs

    def _Run(self):
        if self.do_ssh:
            ExecNode(self.cmd, **self.kwargs).Run()
        else:
            self._LocalRun()

    @abstractmethod
    def _LocalRun(self):
        return
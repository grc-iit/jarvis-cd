
from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.basic.exec_node import ExecNode
from abc import ABC,abstractmethod

class NodeExecNode(ParallelNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._SetKwargs(ParallelNode, kwargs, print_output = False, shell = False)

    def _Run(self):
        if self.do_ssh:
            # We ignore kwargs since we don't want to call SSH during _LocalRun
            cmd = self._ToShellCmd(ignore_params=['kwargs'], set_params={'print_fancy': False})
            node = ExecNode(cmd, **self._GetKwargs(ParallelNode)).Run()
            self.output = node.GetOutput()
        else:
            self._LocalRun()

    @abstractmethod
    def _LocalRun(self):
        return

from jarvis_cd.basic.parallel_node import ParallelNode
from jarvis_cd.basic.exec_node import ExecNode
from abc import ABC,abstractmethod

class NodeExecNode(ParallelNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _Run(self):
        if self.do_ssh:
            # We ignore kwargs since we don't want to call SSH during _LocalRun
            cmd = self._ToShellCmd(ignore_base=ParallelNode, set_params={'print_fancy': False})
            node = ExecNode(cmd, **self.GetClassParams(NodeExecNode, print_output=False, shell=True)).Run()
            self.output = node.GetOutput()
        else:
            self._LocalRun()

    @abstractmethod
    def _LocalRun(self):
        return
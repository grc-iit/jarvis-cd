import sys
from jarvis_cd.shell.ssh_exec_node import SSHExecNode
from jarvis_cd.shell.local_exec_node import LocalExecNode
from jarvis_cd.parallel_node import ParallelNode

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class ExecNode(ParallelNode):
    def __init__(self, cmds, **kwargs):
        super().__init__(**kwargs)

        #Ensure cmds is a list
        if cmds is None:
            cmds = []
        if not isinstance(cmds,list):
            cmds = [cmds]
        if len(cmds) == 0:
            self.cmds = cmds
            return
        cmds = [cmd for cmd in cmds if cmd is not None]
        self.cmds = cmds

    def _Run(self):
        if len(self.cmds) == 0:
            return
        if self.do_ssh:
            node = SSHExecNode(self.cmds, **self.GetClassParams(ParallelNode, print_output=False)).Run()
        else:
            node = LocalExecNode(self.cmds, **self.GetClassParams(ParallelNode, print_output=False)).Run()
        self.output = node.GetOutput()

    def GetCommands(self):
        return self.cmds
import sys
from jarvis_cd.comm.ssh_exec_node import SSHExecNode
from jarvis_cd.basic.local_exec_node import LocalExecNode
from jarvis_cd.basic.parallel_node import ParallelNode

from jarvis_cd.node import Node
from jarvis_cd.exception import Error, ErrorCode

sys.stderr = sys.__stderr__

class ExecNode(ParallelNode):
    def __init__(self, cmds, **kwargs):
        super().__init__(**kwargs)
        self.kwargs = kwargs

        #Ensure cmds is a list
        if not isinstance(cmds,list):
            cmds = [cmds]

        #Ensure this is a list of either only nodes or only strings
        self.is_strlist = all([isinstance(cmd, str) for cmd in cmds])
        self.is_nodelist = all([isinstance(cmd, Node) for cmd in cmds])
        if self.is_strlist and self.is_nodelist:
            raise Error(ErrorCode.INVALID_CMD_LIST).format(cmds)

        #If this is a list of nodes, convert to a python program
        if self.is_nodelist:
            cmds = [ cmd._ToShellCmd() for cmd in cmds]

        #Store command list
        self.cmds = cmds

    def _Run(self):
        if self.do_ssh:
            SSHExecNode(self.name, self.cmds, **self.kwargs)
        else:
            LocalExecNode(self.name, self.cmds, **self.kwargs)
import sys
from jarvis_cd.ssh.openssh.ssh_exec_node import SSHExecNode
from jarvis_cd.shell.local_exec_node import LocalExecNode
from jarvis_cd.parallel_node import ParallelNode

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
            self.node = SSHExecNode(self.cmds, **self.GetClassParams(ParallelNode, print_output=False)).Run()
        else:
            self.node = LocalExecNode(self.cmds, **self.GetClassParams(ParallelNode, print_output=False)).Run()
        self.output = self.node.GetOutput()

    def Wait(self):
        if self.exec_async and not self.do_ssh:
            self.node.Wait()

    def GetCommands(self):
        return self.cmds

from jarvis_cd.shell.exec_node import ExecNode

class MvNode(ExecNode):
    def __init__(self, src, dst, **kwargs):
        self.src = src
        self.dst = dst
        cmds = f"mv {src} {dst}"
        super().__init__(cmds, **kwargs)
from jarvis_cd.shell.exec_node import ExecNode

class LinkNode(ExecNode):
    def __init__(self, src, dst, **kwargs):
        self.src = src
        self.dst = dst
        cmd = f"ln -s {self.src} {self.dst}"
        super().__init__(cmd, **kwargs)
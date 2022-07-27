
from jarvis_cd.shell.exec_node import ExecNode

class MkdirNode(ExecNode):
    def __init__(self, paths, **kwargs):
        if paths is None:
            paths = []
        if not isinstance(paths, list):
            paths = [paths]

        self.paths = paths
        cmds = [f"mkdir -p {path}" for path in paths]
        super().__init__(cmds, **kwargs)
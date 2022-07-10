
from jarvis_cd.basic.exec_node import ExecNode

class RmNode(ExecNode):
    def __init__(self, paths, **kwargs):
        if paths is None:
            paths = []
        if not isinstance(paths, list):
            paths = [paths]

        self.paths = paths
        cmds = [f"rm -rf {path}" for path in paths]
        super().__init__(cmds, **kwargs)
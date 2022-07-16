from jarvis_cd.shell.exec_node import ExecNode
from enum import Enum


class GitOps(Enum):
    CLONE='clone'
    UPDATE='update'

class GitNode(ExecNode):
    def __init__(self, url, path, method, branch=None, commit=None, **kwargs):
        self.url = url
        self.branch = branch
        self.commit = commit
        self.method = method
        self.path = path

        cmds = []
        if self.method == GitOps.CLONE:
            cmds.append(f"git clone {self.url} {self.path}")
        cmds.append(f"cd {self.path}")
        if self.branch is not None:
            cmds.append(f"git switch {self.branch}")
        if self.commit is not None:
            cmds.append(f"git switch {self.commit}")
        if self.method == GitOps.UPDATE:
            cmds.append(f"git pull")

        super().__init__(cmds, **kwargs, shell=True)

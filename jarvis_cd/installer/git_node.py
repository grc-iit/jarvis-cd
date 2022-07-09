
from jarvis_cd.node import Node
from jarvis_cd.basic.exec_node import ExecNode
from enum import Enum
import sys,os

class GitOps(Enum):
    CLONE='clone'
    UPDATE='update'

class GitNode(Node):
    def __init__(self, name, url, path, method, branch=None, commit=None, collect_output=True, print_output=True):
        super.__init__(name, collect_output=collect_output, print_output=print_output)
        self.url = url
        self.branch = branch
        self.commit = commit
        self.method = method
        self.path = path

    def _Run(self):
        cmds = []
        if self.method == GitOps.CLONE:
            cmds.append(f"git clone {self.url} {self.path}")
        cmds.append(f"cd {self.path}")
        if self.branch is not None:
            cmds.append(f"git switch {self.branch}")
        if self.commit is not None:
            ExecNode('switch to commit', f"git switch {self.commit}").Run()
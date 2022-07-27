
from jarvis_cd.node import Node
from jarvis_cd.shell.exec_node import ExecNode
import os

class LocalPipNode(Node):
    def __init__(self, path, inplace=True, user=True, **kwargs):
        super().__init__(**kwargs)
        self.path = path
        self.inplace = inplace
        self.user = user

    def _Run(self):
        cmd = ['python3 -m pip', 'install']
        if self.inplace:
            cmd.append('-e')
        cmd.append(f"{self.path}")
        requirements = os.path.join(self.path, 'requirements.txt')
        if os.path.exists(requirements):
            cmd.append(f"-r {requirements}")
        if self.user:
            cmd.append('--user')
        cmd = ' '.join(cmd)
        ExecNode(cmd).Run()
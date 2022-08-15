
import shutil

from jarvis_cd.basic.node import *

class CheckCommandNode(Node):
    def __init__(self, program, **kwargs):
        super().__init__(**kwargs)
        self.program = program

    def _Run(self):
        self.path = shutil.which(self.program)
        self.exists = self.path is not None

    def Exists(self):
        return self.exists

    def Path(self):
        return self.path
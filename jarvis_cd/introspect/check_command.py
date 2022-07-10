
import shutil

from jarvis_cd.node import *

class CheckCommandNode(Node):
    def __init__(self, program, print_output=True, collect_output=True):
        super().__init__(print_output, collect_output)
        self.program = program

    def _Run(self):
        self.path = shutil.which(self.program)
        self.exists = self.path is not None

    def Exists(self):
        return self.exists

    def Path(self):
        return self.path
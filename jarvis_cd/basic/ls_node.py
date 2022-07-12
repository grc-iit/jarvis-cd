#from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.node import Node
import sys,os
from jarvis_cd.enumerations import Color, OutputStream

class LsNode(Node):
    def __init__(self, path, recurse=True, print_output=False, **kwargs):
        super().__init__(**kwargs, print_output=print_output)
        self.path = path
        self.recurse = True

    def _Run(self):
        self.files = []
        self.dirs = []
        self._Walk(self.path)
        self.AddOutput(self.files, stream=OutputStream.STDOUT)
        self.AddOutput(self.dirs, stream=OutputStream.STDOUT)

    def _Walk(self, root):
        for root,dirs,files in os.walk(root, topdown=False):
            self.dirs += [os.path.join(root,dir) for dir in dirs]
            self.files += [os.path.join(root,file) for file in files]
            if self.recurse:
                for dir in dirs:
                    self._Walk(os.path.join(root, dir))

    def GetFiles(self):
        return self.files

    def GetDirs(self):
        return self.dirs
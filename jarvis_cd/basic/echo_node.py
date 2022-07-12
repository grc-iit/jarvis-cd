from jarvis_cd.node import Node
from jarvis_cd.enumerations import Color, OutputStream

class EchoNode(Node):
    def __init__(self, message, color=None):
        super().__init__(print_output=True)
        self.message = message
        self.color = color

    def _Run(self):
        self.AddOutput(self.message,  color=self.color)
        return self

    def __str__(self):
        return "EchoNode {}".format(self.name)
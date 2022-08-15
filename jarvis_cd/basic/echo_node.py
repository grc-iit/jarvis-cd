from jarvis_cd.basic.node import Node
from jarvis_cd.basic.enumerations import Color, OutputStream

class EchoNode(Node):
    def __init__(self, message, color=None, **kwargs):
        super().__init__(print_output=True, **kwargs)
        self.message = message
        self.color = color

    def _Run(self):
        self.AddOutput(self.message,  color=self.color)
        return self

    def __str__(self):
        return "EchoNode {}".format(self.name)

class ErrorNode(Node):
    def __init__(self, message, color=None, **kwargs):
        super().__init__(print_output=True, **kwargs)
        self.message = message
        self.color = color

    def _Run(self):
        self.AddOutput(self.message,  color=self.color, stream=OutputStream.STDERR)
        return self

    def __str__(self):
        return "EchoNode {}".format(self.name)
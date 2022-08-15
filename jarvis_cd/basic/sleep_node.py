import time
from jarvis_cd.basic.node import Node
from jarvis_cd.basic.enumerations import Color, OutputStream

class SleepNode(Node):
    def __init__(self,  timer, **kwargs):
        super().__init__(**kwargs)
        self.timer = timer

    def _Run(self):
        time.sleep(self.timer)
        self.AddOutput(f"Sleep for {self.timer} seconds", stream=OutputStream.STDOUT)
        return self

    def __str__(self):
        return "SleepNode {}".format(self.name)


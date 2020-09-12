import time

from jarvis_cd.node import Node


class SleepNode(Node):
    def __init__(self, timer, print_output=False):
        super().__init__(print_output)
        self.timer = timer

    def Run(self):
        time.sleep(self.timer)
        output = "Sleep for {} seconds".format(self.timer)
        if self.print_output:
            print(output)
        return output


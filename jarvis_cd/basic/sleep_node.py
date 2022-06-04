import time

from jarvis_cd.node import Node


class SleepNode(Node):
    def __init__(self, name,  timer, print_output=False):
        super().__init__(name, print_output)
        self.timer = timer

    def Run(self):
        time.sleep(self.timer)
        self.output = {
            'localhost': {
                'stdout': ["Sleep for {} seconds".format(self.timer)],
                'stderr': []
            }
        }
        return self

    def __str__(self):
        return "SleepNode {}".format(self.name)


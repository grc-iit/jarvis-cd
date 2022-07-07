from jarvis_cd.node import Node


class EchoNode(Node):
    def __init__(self, name, message):
        super().__init__(name, print_output=True)
        self.message = message

    def _Run(self):
        self.output = [{
            'localhost':{
                'stdout':[self.message],
                'stderr':[]
            }
        }]
        return self

    def __str__(self):
        return "EchoNode {}".format(self.name)
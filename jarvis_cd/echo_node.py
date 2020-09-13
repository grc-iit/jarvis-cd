from jarvis_cd.node import Node


class EchoNode(Node):
    def __init__(self, message):
        super().__init__(print_output=True)
        self.message = message

    def Run(self):
        output={
            'localhost':{
                'stdout':[self.message],
                'stderr':[]
            }
        }
        if self.print_output:
            self.Print(output)
        return output

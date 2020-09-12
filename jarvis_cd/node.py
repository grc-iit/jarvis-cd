from abc import ABC, abstractmethod


class Node(ABC):
    def __init__(self, print_output=False):
        self.print_output = print_output
        pass

    @abstractmethod
    def Run(self):
        pass
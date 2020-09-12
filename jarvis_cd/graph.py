import configparser
from abc import ABC, abstractmethod

class Graph(ABC):
    def __init__(self, config_file = None):
        self.nodes = None
        self.config = configparser.ConfigParser()
        if config_file:
            self.config.read(config_file)

    @abstractmethod
    def _Define(self):
        pass

    def Execute(self):
        nodes = self._Define()
        if type(nodes) == list:
            self.nodes = nodes
        else:
            self.nodes = [nodes]
        output = []*len(self.nodes)
        for i, node in enumerate(self.nodes):
            output[i] = node.Run()
        return output





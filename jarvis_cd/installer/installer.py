from abc import ABC,abstractmethod
import os

class Installer(ABC):
    def __init__(self, config, jarvis_env, jarvis_root, jarvis_shared, jarvis_per_node):
        self.config = config
        self.jarvis_env = jarvis_env
        self.jarvis_root = jarvis_root
        self.jarvis_shared = jarvis_shared
        self.jarvis_per_node = jarvis_per_node

    @abstractmethod
    def LocalInstall(self):
        pass

    @abstractmethod
    def LocalUpdate(self):
        pass

    @abstractmethod
    def LocalUninstall(self):
        pass
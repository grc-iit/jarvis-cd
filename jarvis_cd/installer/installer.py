from abc import ABC,abstractmethod

class Installer(ABC):
    def __init__(self, config, jarvis_env):
        self.config = config
        self.jarvis_env = jarvis_env

    @abstractmethod
    def LocalInstall(self):
        pass

    @abstractmethod
    def LocalUpdate(self):
        pass

    @abstractmethod
    def LocalUninstall(self):
        pass
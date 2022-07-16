from abc import ABC,abstractmethod

class Installer(ABC):
    @abstractmethod
    def LocalInstall(self):
        pass

    @abstractmethod
    def LocalUpdate(self):
        pass

    @abstractmethod
    def LocalUninstall(self):
        pass
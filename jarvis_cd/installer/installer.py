from abc import ABC,abstractmethod
import envbash
import os

class Installer(ABC):
    def __init__(self, config, jarvis_env, jarvis_root, jarvis_shared_pkg_dir, jarvis_per_node_pkg_dir):
        self.config = config
        self.jarvis_env = jarvis_env
        self.jarvis_root = jarvis_root
        self.jarvis_shared_pkg_dir = jarvis_shared_pkg_dir
        self.jarvis_per_node_pkg_dir = jarvis_per_node_pkg_dir
        envbash.load_envbash(self.jarvis_env)

    @abstractmethod
    def LocalInstall(self):
        pass

    @abstractmethod
    def LocalUpdate(self):
        pass

    @abstractmethod
    def LocalUninstall(self):
        pass
from abc import ABC, abstractmethod
from jarvis_cd.basic.jarvis_manager import JarvisManager
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.util.expand_paths import ExpandPaths
import os

from jarvis_cd.basic.exception import Error, ErrorCode

class YAMLConfig(ABC):
    def __init__(self):
        self.config = None

    def LoadConfig(self, config_path):
        self.config_path = config_path
        if not os.path.exists(self.config_path):
            self.is_scaffolded = False
            return self
        self.is_scaffolded = True
        self.config = YAMLFile(self.config_path).Load()
        if 'SHARED_DIR' in self.config and self.config['SHARED_DIR'] is not None:
            os.environ['SHARED_DIR'] = str(self.config['SHARED_DIR'])
        if 'PER_NODE_DIR' in self.config and self.config['PER_NODE_DIR'] is not None:
            os.environ['PER_NODE_DIR'] = str(self.config['PER_NODE_DIR'])
        self.config = ExpandPaths(self.config).Run()
        self._ProcessConfig()
        return self

    def Get(self):
        return self.config

    def SetConfig(self, config):
        if isinstance(config, YAMLConfig):
            self.config = config.config
        else:
            self.config = config
        self.config = ExpandPaths(self.config).Run()
        self._ProcessConfig()
        return self

    @abstractmethod
    def _ProcessConfig(self):
        pass
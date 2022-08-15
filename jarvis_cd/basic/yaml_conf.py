from abc import ABC, abstractmethod
from jarvis_cd.basic.jarvis_manager import JarvisManager
from jarvis_cd.serialize.yaml_file import YAMLFile
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
        self.config = self._ExpandPaths()
        self._ProcessConfig()
        return self

    def Get(self):
        return self.config

    def SetConfig(self, config):
        if isinstance(config, YAMLConfig):
            self.config = config.config
        else:
            self.config = config
        self.config = self._ExpandPaths()
        self._ProcessConfig()
        return self

    def _ExpandPath(self, path):
        return os.path.expandvars(path)

    def _ExpandDict(self, dict_var):
        return {key : self._ExpandVar(var) for key,var in dict_var.items()}

    def _ExpandList(self, list_var):
        return [self._ExpandVar(var) for var in list_var]

    def _ExpandVar(self, var):
        if isinstance(var, dict):
            return self._ExpandDict(var)
        if isinstance(var, list):
            return self._ExpandList(var)
        if isinstance(var, str):
            return self._ExpandPath(var)
        else:
            return var

    def _ExpandPaths(self):
        return self._ExpandVar(self.config)

    @abstractmethod
    def _ProcessConfig(self):
        pass
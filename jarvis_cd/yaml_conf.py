from abc import ABC, abstractmethod
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.serialize.yaml_file import YAMLFile
import os

from jarvis_cd.exception import Error, ErrorCode

class YAMLConfig(ABC):
    def __init__(self, scaffold_dir=None):
        self.scaffold_dir = scaffold_dir
        if self.scaffold_dir is None:
            self.scaffold_dir = os.getcwd()
        self.config_path = None
        self.config = None
        self.jarvis_root = JarvisManager.GetInstance().GetJarvisRoot()

    def ScaffoldConfigPath(self):
        return os.path.join(self.scaffold_dir, 'jarvis_conf.yaml')

    def LoadConfig(self):
        if not os.path.exists(self.ScaffoldConfigPath()):
            return
        self.config = YAMLFile(self.config_path).Load()
        if 'SCAFFOLD' in self.config and self.config['SCAFFOLD'] is not None:
            os.environ['SCAFFOLD'] = str(self.config['SCAFFOLD'])
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
    def DefaultConfigPath(self, conf_type='default'):
        pass

    @abstractmethod
    def _Scaffold(self):
        pass

    @abstractmethod
    def _ProcessConfig(self):
        pass
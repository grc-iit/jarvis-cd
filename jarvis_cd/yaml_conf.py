import yaml
from abc import ABC, abstractmethod
from jarvis_cd.jarvis_manager import JarvisManager
import pathlib
import os
import shutil
import logging
import shutil

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
        if self.config_path is None:
            if os.path.exists(self.ScaffoldConfigPath()):
                self.config_path = self.ScaffoldConfigPath()
            else:
                self.config_path = self.DefaultConfigPath()
        if not os.path.exists(self.config_path):
            raise Error(ErrorCode.INVALID_DEFAULT_CONFIG).format(self.launcher_name)
        with open(self.config_path, "r") as fp:
            self.config = yaml.load(fp, Loader=yaml.FullLoader)
        if 'SCAFFOLD' in self.config:
            os.environ['SCAFFOLD'] = str(self.config['SCAFFOLD'])
        self.config = self._ExpandPaths()
        self._ProcessConfig()
        return self

    def Get(self):
        return self.config

    def Scaffold(self, conf_type='default'):
        old_conf_path = self.DefaultConfigPath(conf_type)
        new_conf_path = self.ScaffoldConfigPath()
        with open(old_conf_path, "r") as old_fp:
            with open(new_conf_path, 'w') as new_fp:
                conf = yaml.load(old_fp, Loader=yaml.FullLoader)
                conf['SCAFFOLD'] = self.scaffold_dir
                yaml.dump(conf, new_fp)

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
    def _ProcessConfig(self):
        return []
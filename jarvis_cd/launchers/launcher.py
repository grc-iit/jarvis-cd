import yaml
from abc import ABC, abstractmethod
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.yaml_conf import YAMLConfig
import pathlib
import os
import shutil
import logging
import shutil

from jarvis_cd.exception import Error, ErrorCode

class Launcher(YAMLConfig):
    def __init__(self, launcher_name, scaffold_dir, args):
        super().__init__(scaffold_dir)
        self.launcher_name = launcher_name
        self.nodes = None
        self.args = args

    def DefaultConfigPath(self, conf_type='default'):
        return os.path.join(self.jarvis_root, 'jarvis_cd', 'launchers', self.launcher_name, f'{conf_type}.yaml')

    @abstractmethod
    def _DefineInit(self):
        return []

    @abstractmethod
    def _DefineStart(self):
        return []

    @abstractmethod
    def _DefineStop(self):
        return []

    @abstractmethod
    def _DefineClean(self):
        return []

    @abstractmethod
    def _DefineStatus(self):
        return []

    def Init(self):
        self._DefineInit()

    def Start(self):
        self._DefineStart()

    def Stop(self):
        self._DefineStop()

    def Clean(self):
        self._DefineClean()

    def Status(self):
        self._DefineStatus()

    def Restart(self):
        self.Stop()
        self.Start()

    def Reset(self):
        self.Stop()
        self.Clean()
        self.Init()
        self.Start()

    def Destroy(self):
        self.Stop()
        self.Clean()

    def Setup(self):
        self.Init()
        self.Start()

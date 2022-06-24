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

class BootstrapConfig(YAMLConfig):
    def DefaultConfigPath(self, conf_type='remote'):
        return os.path.join(self.jarvis_root, 'jarvis_cd', 'bootstrap', f'{conf_type}.yaml')

    def _ProcessConfig(self):
        pass
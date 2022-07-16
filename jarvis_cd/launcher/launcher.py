from abc import abstractmethod
from jarvis_cd.comm.ssh_config import SSHConfigMixin
from jarvis_cd.yaml_cache import YAMLCache
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.jarvis_manager import JarvisManager
import os
import inspect

class Launcher(SSHConfigMixin,YAMLCache):
    def __init__(self, scaffold_dir):
        super().__init__(scaffold_dir)
        self.nodes = None
        self.cache = self.LoadCache()

    def DefaultConfigPath(self, conf_type='default'):
        launcher_path = os.path.dirname(inspect.getfile(self.__class__))
        return os.path.join(launcher_path, 'conf', f'{conf_type}.yaml')

    def Scaffold(self, conf_type='default'):
        old_conf_path = self.DefaultConfigPath(conf_type)
        new_conf_path = self.ScaffoldConfigPath()
        self.config = YAMLFile(old_conf_path).Load()
        self.config['SCAFFOLD'] = self.scaffold_dir
        self.config = self._ExpandPaths()
        self._Scaffold()
        YAMLFile(new_conf_path).Save(self.config)
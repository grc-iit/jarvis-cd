from jarvis_cd.ssh.ssh_config import SSHConfigMixin
from jarvis_cd.yaml_cache import YAMLCache
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.jarvis_manager import JarvisManager
import os
import inspect
import random
import datetime
import time

class Launcher(SSHConfigMixin,YAMLCache):
    def __init__(self, scaffold_dir):
        super().__init__(scaffold_dir)
        self.nodes = None
        self.cache = self.LoadCache()
        self.jarvis_root = JarvisManager().GetJarvisRoot()
        self.jarvis_env = os.path.join(self.jarvis_root, '.jarvis_env')

    def GetEnv(self):
        class_name = type(self).__name__
        filename = f"{class_name}-{self.config['Z_UUID']}"
        return os.path.join(self.jarvis_root, 'jarvis_envs', os.environ['USER'], filename)

    def DefaultConfigPath(self, conf_type='default'):
        launcher_path = os.path.dirname(inspect.getfile(self.__class__))
        return os.path.join(launcher_path, 'conf', f'{conf_type}.yaml')

    def Scaffold(self, conf_type='default'):
        old_conf_path = self.DefaultConfigPath(conf_type)
        new_conf_path = self.ScaffoldConfigPath()
        self.config = YAMLFile(old_conf_path).Load()

        nonce = str(random.randrange(0,2**64))
        date = str(datetime.datetime.now())
        t = str(time.process_time())
        config_uuid = hash(nonce + date + t)

        self.config['Z_UUID'] = config_uuid
        self.config['SCAFFOLD'] = self.scaffold_dir
        self.config = self._ExpandPaths()
        self._Scaffold()
        YAMLFile(new_conf_path).Save(self.config)

    def _ScaffoldArgs(self, parser):
        parser.add_argument('conf_type', metavar='type', help='the configuration file to load')
from abc import ABC, abstractmethod
from jarvis_cd.ssh.ssh_config_mixin import SSHConfigMixin
from jarvis_cd.yaml_cache import YAMLCacheMixin
from jarvis_cd.basic_env import BasicEnvMixin
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.hostfile import Hostfile
import os
import envbash
import inspect
import random
import datetime
import time
import inspect

class Launcher(SSHConfigMixin,YAMLCacheMixin,BasicEnvMixin):
    def __init__(self, scaffold_dir=None, pkg_id=None):
        super().__init__()
        self.nodes = None
        self.cache = self.LoadCache()
        self.env = None
        self.jarvis_manager = JarvisManager().GetInstance()
        self.jarvis_root = self.jarvis_manager.GetJarvisRoot()
        self.jarvis_shared_pkg_dir = self.jarvis_manager.GetPkgInstanceDir(shared=True)
        self.jarvis_per_node_pkg_dir = self.jarvis_manager.GetPkgInstanceDir(shared=False)
        self.jarvis_env = os.path.join(self.jarvis_root, '.jarvis_env')
        self.package_root = os.path.dirname(inspect.getfile(self.__class__))
        self.shared_dir = None
        self.shared_exists = False
        self.per_node_dir = None
        self.per_node_exists = False
        envbash.load_envbash(self.jarvis_env)

        #Locate the scaffold directory
        if scaffold_dir is not None:
            self.scaffold_dir = scaffold_dir
        else:
            self.scaffold_dir = os.getcwd()

        #Get package id based on scaffold directory name
        if pkg_id is None:
            pkg_root = os.path.dirname(self.scaffold_dir)
            if pkg_root == self.jarvis_shared_pkg_dir:
                pkg_id = os.path.basename(self.scaffold_dir)
            if pkg_root == self.jarvis_per_node_pkg_dir:
                pkg_id = os.path.basename(self.scaffold_dir)

        #Some content needs to be stored per-node
        #Other content may be stored in a shared directory
        if self.jarvis_per_node_pkg_dir is not None and pkg_id is not None:
            self.shared_dir = os.path.join(self.jarvis_per_node_pkg_dir, pkg_id)
            self.shared_exists = os.path.exists(self.shared_exists)
        if self.jarvis_shared_pkg_dir is not None and pkg_id is not None:
            self.per_node_dir = os.path.join(self.jarvis_shared_pkg_dir, pkg_id)
            self.per_node_exists = os.path.exists(self.per_node_exists)

        #Load the configuration file from the scaffold directory
        self.LoadConfig(os.path.join(self.scaffold_dir, 'jarvis_conf.yaml'))

        #Determine if the scaffold directory is shared or not
        if scaffold_dir == self.shared_dir:
            self.scaffold_hosts = Hostfile().Load(['localhost'])
        elif scaffold_dir == self.per_node_dir:
            self.scaffold_hosts = self.all_hosts
        elif 'SCAFFOLD_SHARED' in self.config:
            if self.config['SCAFFOLD_SHARED']:
                self.scaffold_hosts = Hostfile().Load(['localhost'])
            else:
                self.scaffold_hosts = self.all_hosts
        elif 'HOSTS' in self.config:
            self.scaffold_hosts = self.all_hosts
        else:
            self.scaffold_hosts = Hostfile().Load(['localhost'])


    def DefaultConfigPath(self, conf_type='default'):
        return os.path.join(self.package_root, 'conf', f'{conf_type}.yaml')
    def ScaffoldConfigPath(self):
        return os.path.join(self.scaffold_dir, 'jarvis_conf.yaml')

    def Scaffold(self, conf_type='default', config=None):
        old_conf_path = self.DefaultConfigPath(conf_type)
        new_conf_path = self.ScaffoldConfigPath()
        if config is None:
            config = YAMLFile(old_conf_path).Load()
        self.config = config

        nonce = str(random.randrange(0, 2 ** 64))
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

    def Create(self, pkg_id, conf_type='default'):
        if self.shared_dir:
            MkdirNode(os.path.join(self.shared_dir, pkg_id)).Run()
        if self.per_node_dir:
            MkdirNode(os.path.join(self.per_node_dir, pkg_id), hosts=self.all_hosts).Run()
        self.Scaffold(conf_type)
    def _CreateArgs(self, parser):
        parser.add_argument('pkg_id', metavar='id', help='the human-readable id of this instance of the launcher')
        self._ScaffoldArgs(parser)

    @abstractmethod
    def _Scaffold(self):
        pass
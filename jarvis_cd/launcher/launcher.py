from abc import ABC, abstractmethod
from jarvis_cd.ssh.ssh_config_mixin import SSHConfigMixin
from jarvis_cd.yaml_cache import YAMLCacheMixin
from jarvis_cd.basic_env import BasicEnvMixin
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.exception import Error,ErrorCode
from jarvis_cd.fs.mkdir_node import MkdirNode
import os
import envbash
import inspect
import random
import datetime
import time
import inspect

class Launcher(SSHConfigMixin,YAMLCacheMixin,BasicEnvMixin):
    def __init__(self, scaffold_dir=None, pkg_id=None, load_config=True):
        super().__init__()
        self.nodes = None
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
        self.scaffold_dir = None
        self.scaffold_hosts = None
        envbash.load_envbash(self.jarvis_env)

        #Get all paths for a package (if it exists)
        if pkg_id is not None:
            self._PackagePathsFromID(pkg_id)
        else:
            self._PackagePathsFromScaffold(scaffold_dir)

        #Load the configuration file from the scaffold directory
        if load_config:
            if self.scaffold_dir is not None:
                self.LoadConfig(self.ScaffoldConfigPath())
                self._FindScaffoldHosts()
            else:
                raise Error(ErrorCode.CONFIG_NOT_FOUND).format(self.scaffold_dir)

        #Load the cache
        self.cache = self.LoadCache()

    def _PackagePathsFromID(self, pkg_id):
        if pkg_id is None:
            return
        # Some content needs to be stored per-node
        # Other content may be stored in a shared directory
        if self.jarvis_per_node_pkg_dir is not None and pkg_id is not None:
            self.shared_dir = os.path.join(self.jarvis_per_node_pkg_dir, pkg_id)
            self.shared_exists = os.path.exists(self.shared_dir)
        if self.jarvis_shared_pkg_dir is not None and pkg_id is not None:
            self.per_node_dir = os.path.join(self.jarvis_shared_pkg_dir, pkg_id)
            self.per_node_exists = os.path.exists(self.per_node_dir)

        if self.shared_exists:
            self.scaffold_dir = self.shared_dir
            self.scaffold_hosts = Hostfile().Load(['localhost'])
        elif self.per_node_exists:
            self.scaffold_dir = self.per_node_dir
            self.scaffold_hosts = self.all_hosts

    def _PackagePathsFromScaffold(self, scaffold_dir):
        #Locate the scaffold directory
        if scaffold_dir is not None:
            self.scaffold_dir = scaffold_dir
        else:
            self.scaffold_dir = os.getcwd()

        #Get package id based on scaffold directory name
        pkg_id = None
        pkg_root = os.path.dirname(self.scaffold_dir)
        if pkg_root == self.jarvis_shared_pkg_dir:
            pkg_id = os.path.basename(self.scaffold_dir)
        if pkg_root == self.jarvis_per_node_pkg_dir:
            pkg_id = os.path.basename(self.scaffold_dir)
        self._PackagePathsFromID(pkg_id)

    def _FindScaffoldHosts(self):
        #Determine if the scaffold directory is shared or not
        if self.scaffold_dir == self.shared_dir:
            self.scaffold_hosts = None
        elif self.scaffold_dir == self.per_node_dir:
            self.scaffold_hosts = self.all_hosts
        elif self.config is not None and 'SCAFFOLD_SHARED' in self.config:
            if self.config['SCAFFOLD_SHARED']:
                self.scaffold_hosts = None
            else:
                self.scaffold_hosts = self.all_hosts
        elif self.config is not None and 'HOSTS' in self.config:
            self.scaffold_hosts = self.all_hosts
        else:
            self.scaffold_hosts = None

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
        self.config['SHARED_DIR'] = self.shared_dir
        self.config['PER_NODE_DIR'] = self.per_node_dir
        self.config = self._ExpandPaths()
        self._Scaffold()
        YAMLFile(new_conf_path).Save(self.config)
    def _ScaffoldArgs(self, parser):
        parser.add_argument('conf_type', metavar='type', help='the configuration file to load')

    def Create(self, pkg_id, conf_type='default'):
        self._PackagePathsFromID(pkg_id)
        if self.shared_dir is not None:
            if not os.path.exists(self.shared_dir):
                MkdirNode(self.shared_dir).Run()
        if self.per_node_dir is not None:
            if not os.path.exists(self.per_node_dir):
                MkdirNode(self.per_node_dir).Run()
        if not self.shared_exists and not self.per_node_exists:
            raise Error(ErrorCode.JARVIS_PKG_NOT_CONFIGURED).format()
        self.Scaffold(conf_type)
    def _CreateArgs(self, parser):
        parser.add_argument('pkg_id', metavar='id', help='the human-readable id of this instance of the launcher')
        self._ScaffoldArgs(parser)

    #@abstractmethod
    def _Scaffold(self):
        pass
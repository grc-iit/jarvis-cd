import yaml
from abc import ABC, abstractmethod
from jarvis_cd.jarvis_manager import JarvisManager
import pathlib
import os
import shutil
import logging
import shutil

from jarvis_cd.exception import Error, ErrorCode

class LauncherConfig(ABC):
    def __init__(self, launcher_name, config_path=None):
        self.launcher_name = launcher_name
        self.config_path = config_path
        self.config = None

    def DefaultConfigPath(self):
        return JarvisManager.GetInstance().GetDefaultConfigPath(self.launcher_name)

    def LoadConfig(self):
        if self.config_path is None:
            self.config_path = self.DefaultConfigPath()
        if not os.path.exists(self.config_path):
            raise Error(ErrorCode.INVALID_DEFAULT_CONFIG).format(self.launcher_name)
        with open(self.config_path, "r") as fp:
            self.config = yaml.load(fp.read())
        if 'SCAFFOLD' in self.config:
            os.environ['SCAFFOLD'] = str(self.config['SCAFFOLD'])
        self._ProcessConfig()

    def SetConfig(self, config):
        self.config = config
        self._ProcessConfig()

    def _ExpandPath(self, path):
        return os.path.expandvars(path)

    @abstractmethod
    def _ProcessConfig(self):
        return []

class Launcher(LauncherConfig):
    def __init__(self, launcher_name, config_path, args):
        super().__init__(launcher_name, config_path)
        self.nodes = None
        self.args = args
        self.SetTempDir("{}_{}".format(JarvisManager.GetInstance().GetTmpDir(), launcher_name))

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

    def _ExecuteNodes(self, nodes):
        if type(nodes) == list:
            self.nodes = nodes
        else:
            self.nodes = [nodes]
        outputs = []
        if len(self.nodes) > 0:
            outputs = []
            for i, node in enumerate(self.nodes):
                logging.info("Executing node {} index {}".format(str(node),i))
                output = node.Run()
                outputs.append(output)
        return outputs

    def SetTempDir(self, temp_dir):
        self.temp_dir = temp_dir
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)

    def Scaffold(self):
        old_conf_path = self.DefaultConfigPath()
        new_conf_path = os.path.join(os.getcwd(), 'conf.yaml')
        with open(old_conf_path, "r") as old_fp:
            with open(new_conf_path, 'w') as new_fp:
                conf = yaml.load(old_fp, Loader=yaml.FullLoader)
                conf['SCAFFOLD'] = os.getcwd()
                yaml.dump(conf, new_fp)

    def Init(self):
        nodes = self._DefineInit()
        return self._ExecuteNodes(nodes)

    def Start(self):
        nodes = self._DefineStart()
        return self._ExecuteNodes(nodes)

    def Stop(self):
        nodes = self._DefineStop()
        return self._ExecuteNodes(nodes)

    def Clean(self):
        nodes = self._DefineClean()
        return self._ExecuteNodes(nodes)

    def Status(self):
        nodes = self._DefineStatus()
        return self._ExecuteNodes(nodes)

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

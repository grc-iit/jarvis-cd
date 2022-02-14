import configparser
from abc import ABC, abstractmethod
from jarvis_cd.jarvis_manager import JarvisManager
import pathlib
import os
import shutil
import logging

from jarvis_cd.exception import Error, ErrorCode

class LauncherConfig(ABC):
    def __init__(self, launcher_name, config_path=None):
        self.launcher_name = launcher_name
        self.config = {}
        self._LoadDefaultConfig()
        if config_path is not None:
            self.LoadConfig(config_path)

    def _LoadDefaultConfig(self):
        default_config_path = os.path.join(JarvisManager.GetInstance().GetLauncherPath(self.launcher_name), 'default.ini')
        if not os.path.exists(default_config_path):
            raise Error(ErrorCode.INVALID_DEFAULT_CONFIG).format(self.launcher_name)
        default_config = configparser.ConfigParser()
        default_config.read(default_config_path)
        for section in default_config.sections():
            if section not in self.config:
                self.config[section] = {}
            for key in default_config[section]:
                self.config[section][key.upper()] = os.path.expandvars(default_config[section][key])
        self._LoadConfig()

    def LoadConfig(self, config_path):
        if config_path is None:
            return None
        if not os.path.exists(config_path):
            raise Error(ErrorCode.CONFIG_NOT_FOUND).format(config_path)
        user_config = configparser.ConfigParser()
        user_config.read(config_path)
        for section in user_config.sections():
            if section not in self.config:
                raise Error(ErrorCode.INVALID_SECTION).format(section, self.config.keys())
            for key in user_config[section]:
                if key not in self.config[section]:
                    raise Error(ErrorCode.INVALID_KEY).format(key, self.config[section].keys())
                self.config[section][key.upper()] = os.path.expandvars(user_config[section][key])
        self._LoadConfig()
        return self

    @abstractmethod
    def _LoadConfig(self):
        return []

class Launcher(LauncherConfig):
    def __init__(self, launcher_name, config_path, args):
        super().__init__(launcher_name, config_path)
        self.nodes = None
        self.args = args
        self.SetTempDir("{}_{}".format(JarvisManager.GetInstance().GetTmpDir(), launcher_name))

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

    def SetTempDir(self, temp_dir):
        self.temp_dir = temp_dir
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)

    def Restart(self):
        self.Stop()
        self.Start()

    def Clean(self):
        nodes = self._DefineClean()
        if type(nodes) == list:
            self.nodes = nodes
        else:
            self.nodes = [nodes]
        output = []
        if len(self.nodes) > 0:
            output = []
            for i, node in enumerate(self.nodes):
                logging.info("Executing node {} index {}".format(str(node), i))
                output.append(node.Run())
        return output

    def Stop(self):
        nodes = self._DefineStop()
        if type(nodes) == list:
            self.nodes = nodes
        else:
            self.nodes = [nodes]
        output = []
        if len(self.nodes) > 0:
            output = []
            for i, node in enumerate(self.nodes):
                logging.info("Executing node {} index {}".format(str(node),i))
                output.append(node.Run())
        return output

    def Start(self):
        nodes = self._DefineStart()
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

    def Status(self):
        nodes = self._DefineStatus()
        if type(nodes) == list:
            self.nodes = nodes
        else:
            self.nodes = [nodes]
        outputs = []
        if len(self.nodes) > 0:
            outputs = []
            for i, node in enumerate(self.nodes):
                logging.info("Executing node {} index {}".format(str(node), i))
                output = node.Run()
                outputs.append(output)
        return outputs





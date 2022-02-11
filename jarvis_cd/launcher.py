import configparser
from abc import ABC, abstractmethod
from jarvis_cd.jarvis_manager import JarvisManager
import pathlib
import os
import shutil
import logging

from jarvis_cd.exception import Error, ErrorCode

class LauncherConfig:
    def __init__(self, launcher_name):
        self.launcher_name = launcher_name
        self.config = {}
        self._LoadDefaultConfig()

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

    def __getitem__(self, key):
        return self.config[key]

class Launcher(ABC):
    def __init__(self, config=None, args=None):
        self.nodes = None
        self.args = args
        self.SetConfig(config)

    def _convert_hostfile_tolist(self, filename):
        if not os.path.exists(filename):
            raise Error(ErrorCode.HOSTFILE_NOT_FOUND).format(filename)
        a_file = open(filename, "r")
        list_of_lists = []
        for line in a_file:
            stripped_line = line.strip()
            line_list = stripped_line.split(sep=":")
            if len(line_list) == 2:
                for i in range(int(line_list[1])):
                    list_of_lists.append(line_list[0])
            else:
                list_of_lists.append(line_list[0])
        a_file.close()

        return list_of_lists

    @abstractmethod
    def _SetConfig(self):
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

    def SetTempDir(self, temp_dir):
        self.temp_dir = temp_dir
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)

    def SetConfig(self, config):
        self.config = config
        if self.config is None:
            return
        self._SetConfig()

    def GetConfig(self):
        return

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





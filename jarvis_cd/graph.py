import configparser
from abc import ABC, abstractmethod
import pathlib
import os
import shutil
import logging

from jarvis_cd.exception import Error, ErrorCode


class Graph(ABC):
    def __init__(self, config_file = None, default_config=None):
        self.nodes = None
        self.config = configparser.ConfigParser()
        self.temp_dir = "/tmp/jarvis-cd/orangefs"
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
        if default_config:
            self.project_src = "{}".format(pathlib.Path(os.getcwd()).absolute())
            self.config.read(os.path.join(self.project_src, default_config))
            for section in self.config.sections():
                for key in self.config[section]:
                    self.config[section][key] = os.path.expandvars(self.config[section][key])
            if config_file:
                user_config = configparser.ConfigParser()
                user_config.read(config_file)
                for section in user_config.sections():
                    if section not in self.config:
                        raise Error(ErrorCode.INVALID_SECTION).format(section, default_config)
                    for key in user_config[section]:
                        if key not in self.config[section]:
                            raise Error(ErrorCode.INVALID_KEY).format(key, default_config)
                        self.config[section][key] = os.path.expandvars(user_config[section][key])

    def _convert_hostfile_tolist(self, filename):
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
    def _DefineStart(self):
        return []

    @abstractmethod
    def _DefineStop(self):
        return []

    @abstractmethod
    def _DefineStatus(self):
        return []

    def Restart(self):
        self.Stop()
        self.Start()

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





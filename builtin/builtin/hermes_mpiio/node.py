from jarvis_cd.basic.node import Interceptor
from jarvis_util import *


class HermesMpiio(Interceptor):
    def _init(self):
        """
        Initialize paths
        """
        pass

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        return []

    def configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this node.
        :return: None
        """
        self.update_config(kwargs, rebuild=False)
        self.config['HERMES_MPIIO'] = self.find_library('hermes_mpiio')
        if self.config['HERMES_MPIIO'] is None:
            raise Exception("Failed to find hermes_mpiio")

    def modify_env(self):
        """
        Modify the jarvis environment.

        :return: None
        """
        self.prepend_path('LD_PRELOAD', self.config['HERMES_MPIIO'])

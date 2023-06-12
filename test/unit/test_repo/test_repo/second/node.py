from jarvis_cd.basic.node import Interceptor
from jarvis_util import *


class Second(Interceptor):
    def _init(self):
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

        :param config: The human-readable jarvis YAML configuration for the
        application.
        :return: None
        """
        print(f'{kwargs}')

    def modify_env(self):
        """
        Modify the jarvis environment.

        :return: None
        """
        print('second modify_env')

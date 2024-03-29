"""
Second pkg example
"""
from jarvis_cd.basic.pkg import Interceptor
from jarvis_util import *


class Second(Interceptor):
    """
    Interceptor example
    """
    def _init(self):
        pass

    def _configure_menu(self):
        """
        Create a CLI menu for the configurator method.
        For thorough documentation of these parameters, view:
        https://github.com/scs-lab/jarvis-util/wiki/3.-Argument-Parsing

        :return: List(dict)
        """
        return [
            {
                'name': 'port',
                'msg': 'The port to listen for data on',
                'type': int
            },
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param config: The human-readable jarvis YAML configuration for the
        application.
        :return: None
        """
        print(f'{kwargs}')

    def _get_stat(self, stat_dict):
        """
        Get statistics from the application.

        :param stat_dict: A dictionary of statistics.
        :return: None
        """
        stat_dict[f'{self.pkg_id}.runtime'] = self.start_time

    def modify_env(self):
        """
        Modify the jarvis environment.

        :return: None
        """
        print('second modify_env')

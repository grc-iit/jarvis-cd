"""
First pkg example
"""
from jarvis_cd.basic.pkg import Service
from jarvis_util import *


class First(Service):
    """
    Service example
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
                'name': 'walkthrough',
                'msg': 'Use a terminal walkthrough to modify resource graph',
                'type': bool,
                'default': False,
            },
            {
                'name': 'devices',
                'msg': 'Search for a number of devices to include',
                'type': list,
                'default': None,
                'args': [
                    {
                        'name': 'type',
                        'msg': 'The type of the device being queried',
                        'type': str
                    },
                    {
                        'name': 'count',
                        'msg': 'The number of devices being',
                        'type': int
                    }
                ]
            },
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

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        print('first start')

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        print('first stop')

    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        print('first clean')

    def status(self):
        """
        Check whether or not an application is running. E.g., are OrangeFS
        servers running?

        :return: True or false
        """
        print('first status')
        return True

"""
This module provides classes and methods to launch the Hermes2IpcTest application.
Hermes2IpcTest is ....
"""
from jarvis_cd.basic.node import Application
from jarvis_util import *


class Hermes2IpcTest(Application):
    """
    This class provides methods to launch the Hermes2IpcTest application.
    """
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
        return [
            {
                'name': None,  # The name of the parameter
                'msg': '',  # Describe this parameter
                'type': str,  # What is the parameter type?
                'default': None,  # What is the default value if not required?
                # Does this parameter have specific valid inputs?
                'choices': [],
                # When type is list, what do the entries of the list mean?
                # A list of dicts just like this one.
                'args': [],
            },
        ]

    def configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this node.
        :return: None
        """
        self.update_config(kwargs, rebuild=False)

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary nodes.

        :return: None
        """
        pass

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        pass

    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        pass

from jarvis_cd.basic.node import Interceptor
from jarvis_util import *


class MyRepo(Interceptor):
    def __init__(self):
        """
        Initialize paths
        """
        super().__init__()

    def configure_menu(self):
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
        pass

    def modify_env(self):
        """
        Modify the jarvis environment.

        :return: None
        """
        pass

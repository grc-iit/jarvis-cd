"""
This module provides classes and methods to launch the Ior application.
Ior is ....
"""
from jarvis_cd.basic.pkg import Service
from jarvis_util import *
from jarvis_util.introspect.monitor import Monitor

class Pymonitor(Service):
    """
    This class provides methods to launch the Ior application.
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
                'name': 'frequency',
                'msg': 'Monitor frequency in seconds',
                'type': int,
                'default': 1
            },
            {
                'name': 'monitor_dir',
                'msg': 'Directory to store monitor logs',
                'type': bool,
                'default': False,
            },
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        self.config['api'] = self.config['api'].upper()

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        Monitor(self.config['frequency'],
                self.config['monitor_dir'],
                PsshExecInfo(env=self.env,
                            hostfile=self.jarvis.hostfile,
                            exec_async=True))

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        Kill('.*pymonitor.*', PsshExecInfo(env=self.env))

    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        Rm(self.config['monitor_dir'])

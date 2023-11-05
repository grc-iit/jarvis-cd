"""
This module provides classes and methods to launch the Ior application.
Ior is ....
"""
from jarvis_cd.basic.pkg import Application
from jarvis_util import *


class HermesViz(Application):
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
                'name': 'port',
                'msg': 'Por the server will listen at',
                'type': int,
                'default': 5000,
            },
            {
                'name': 'pooling',
                'msg': 'Time in seconds (accepts floats) that server will sleep between pooling hermes',
                'type': float,
                'default': 0.5,
            },
            {
                'name': 'real',
                'msg': 'Generate data or capture from hermes',
                'type': bool,
                'default': True,
            },
            {
                'name': 'hostfile',
                'msg': 'hostfile with nodes under which we are running',
                'type': str,
                'default': "~/jarvis_node_normal",
            },
            {
                'name': 'db_path',
                'msg': 'path to the database to gather the metadata',
                'type': str,
                'default': "/mnt/nvme/jcernudagarcia/metadata.db",
            },
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        pass

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        cmd = f'hermes_viz.py --port {self.config["port"]} --sleep_time {self.config["pooling"]} ' \
              f'--real {self.config["real"]} --hostfile {self.config["hostfile"]} ' \
              f'--db_path {self.config["db_path"]}'
        Exec(cmd, LocalExecInfo())

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

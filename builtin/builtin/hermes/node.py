from jarvis_cd.basic.node import Service
from jarvis_util import *


class Hermes(Service):
    def __init__(self):
        """
        Initialize paths
        """
        super().__init__()

    def configure_help(self):
        args = [
            {
                'name': 'walkthrough',
                'msg': 'Use a terminal walkthrough to modify resource graph',
                'type': bool,
                'default': False,
            },
            {
                'name': 'reinit',
                'msg': 'Destroy previous configuration and rebuild',
                'type': bool,
                'default': False
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
                'msg': 'The port to listen for data on'
            },
        ]
        return args

    def configure(self, config):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param config: The human-readable jarvis YAML configuration for the
        application.
        :return: None
        """
        'devices.nvme.type'

        rg = self.jarvis.resource_graph
        # graphs
        config = {
            'devices': {}
        }
        # Introspect resource graph to find
        if 'devices' in config:
            for dev in config['devices']:
                config['devices'][dev] = rg.find_storage()

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary nodes.

        :return: None
        """
        Exec('hermes_daemon',
             PsshExecInfo(hostfile=self.jarvis.hostfile,
                          env=self.env))

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        Kill('hermes_daemon',
             PsshExecInfo(hostfile=self.jarvis.hostfile,
                          env=self.env))

    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        pass

    def status(self):
        """
        Check whether or not an application is running. E.g., are OrangeFS
        servers running?

        :return: True or false
        """
        return True

from jarvis_cd.basic.node import Service
from jarvis_util import *


class Hermes(Service):
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
                'msg': 'The port to listen for data on',
                'type': int,
                'default': 8080
            },
        ]

    def configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param config: The human-readable jarvis YAML configuration for the
        application.
        :return: None
        """
        self.update_config(kwargs, rebuild=False)
        rg = self.jarvis.resource_graph

        if len(self.config['devices'] == 0):
            # Get all the fastest storage device mount points on machine
            dev_df = rg.find_storage(common=True,
                                     min_cap=SizeConv.to_int('40g'))
            devs = {
                'nvme': dev_df[dev_df.type == StorageDeviceType.NVME],
                'ssd': dev_df[dev_df.type == StorageDeviceType.SSD],
                'hdd': dev_df[dev_df.type == StorageDeviceType.HDD]
            }
        else:
            # Get the storage devices for the user
            devs = {}
            for dev_type, count in self.config['devices']:
                devs[dev_type] = rg.find_storage(common=True,
                                                 dev_types=dev_type,
                                                 count_per_node=count)

        # Get network information
        net_info = rg.find_net_info(self.jarvis.hostfile)
        net_info = net_info[net_info.provider == 'sockets']
        protocol = list(net_info['provier'].unique())[0]
        domain = list(net_info['domain'].unique())[0]

        # Begin making Hermes config
        hermes = {
            'devices': {},
            'rpc': {
                'host_file': self.jarvis.hostfile.path,
                'protocol': protocol,
                'domain': domain,
                'port': self.config['port'],
                'num_threads': 4
            }
        }

        # Storage info


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

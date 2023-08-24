"""
Hermes is an I/O buffering system. This file provides tools to configure
and deploy Hermes alongside an application.
"""

from jarvis_cd.basic.node import Service
#
from jarvis_util import *
import jarvis_util.util.small_df as sdf
import subprocess
import time


class Hermes(Service):
    """
    Provide methods to
    """
    def _init(self):
        """
        Initialize paths
        """
        self.daemon_node = None

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
                'name': 'reinit',
                'msg': 'Destroy previous configuration and rebuild',
                'type': bool,
                'default': False
            },
            {
                'name': 'devices',
                'msg': 'Search for a number of devices to include',
                'type': list,
                'default': [],
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
            {
                'name': 'provider',
                'msg': 'The libfabric provider type to use (e.g., sockets)',
                'type': str,
                'default': None
            },
            {
                'name': 'output_dir',
                'msg': 'Where the application performs I/O',
                'type': str,
                'default': None
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
        self._configure_server()
        self._configure_client()

    def _configure_server(self):
        rg = self.jarvis.resource_graph

        if len(self.config['devices']) == 0:
            # Get all the fastest storage device mount points on machine
            dev_df = rg.find_storage(common=True,
                                     min_cap=SizeConv.to_int('40g'))
        else:
            # Get the storage devices for the user
            dev_list = [rg.find_storage(common=True,
                                        dev_types=dev_type,
                                        count_per_node=count)
                        for dev_type, count in self.config['devices']]
            dev_df = sdf.concat(dev_list)

        # Begin making Hermes config
        hermes_server = {
            'devices': {},
            'rpc': {}
        }

        # Get storage info
        devs = dev_df.rows
        for i, dev in enumerate(devs):
            dev_type = dev['dev_type']
            custom_name = f'{dev_type}_{i}'
            mount = os.path.expandvars(dev['mount'])
            if len(mount) == 0:
                continue
            if dev_type == 'nvme':
                bandwidth = '1g'
                latency = '60us'
            elif dev_type == 'ssd':
                bandwidth = '500MBps'
                latency = '400us'
            elif dev_type == 'hdd':
                bandwidth = '120MBps'
                latency = '5ms'
            else:
                continue
            if dev['avail'] is None:
                dev['avail'] = .6 * dev['size']
            mount = f'{mount}/hermes_data'
            hermes_server['devices'][custom_name] = {
                'mount_point': mount,
                'capacity': int(.9 * float(dev['avail'])),
                'block_size': '4kb',
                'bandwidth': bandwidth,
                'latency': latency,
                'is_shared_device': dev['shared'],
                'borg_capacity_thresh': [0.0, 1.0],
                'slab_sizes': ['4KB', '16KB', '64KB', '1MB']
            }
            Mkdir(mount, PsshExecInfo(hostfile=self.jarvis.hostfile,
                                      env=self.env))

        # Get network Info
        net_info = rg.find_net_info(Hostfile())
        provider = self.config['provider']
        if provider is None:
            opts = net_info['provider'].unique().list()[0]
            order = ['sockets', 'tcp', 'verbs', 'ib']
            print(opts)
            for opt in order:
                if opt in opts:
                    provider = opt
                    break
            if provider is None:
                provider = opts[0]
        net_info = net_info[lambda r: str(r['provider']) == provider,
                            ['provider', 'domain']]
        net_info = net_info.rows[0]
        protocol = net_info['provider']
        domain = net_info['domain']
        hostfile_path = self.jarvis.hostfile.path
        if hostfile_path is None:
            hostfile_path = ''
        hermes_server['rpc'] = {
            'host_file': hostfile_path,
            'protocol': protocol,
            'domain': domain,
            'port': self.config['port'],
            'num_threads': 4
        }
        if self.jarvis.hostfile.path is None:
            hermes_server['rpc']['host_names'] = self.jarvis.hostfile.hosts

        # Save Hermes configurations
        hermes_server_yaml = f'{self.shared_dir}/hermes_server.yaml'
        YamlFile(hermes_server_yaml).save(hermes_server)
        self.env['HERMES_CONF'] = hermes_server_yaml

    def _configure_client(self):
        hermes_client = {
            'stop_daemon': False,
            'path_inclusions': ['/tmp/test_hermes'],
            'path_exclusions': ['/'],
            'file_page_size': '1024KB',
            'base_adapter_mode': 'kDefault',
            'flushing_mode': 'kAsync'
        }
        if self.config['output_dir'] is not None:
            hermes_client['path_inclusions'].append(self.config['output_dir'])
        Mkdir(hermes_client['path_inclusions'],
              PsshExecInfo(hostfile=self.jarvis.hostfile,
                           env=self.env))
        hermes_client_yaml = f'{self.shared_dir}/hermes_client.yaml'
        YamlFile(hermes_client_yaml).save(hermes_client)
        self.env['HERMES_CLIENT_CONF'] = hermes_client_yaml

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary nodes.

        :return: None
        """
        self.daemon_node = Exec('hermes_daemon',
                                LocalExecInfo(hostfile=self.jarvis.hostfile,
                                              env=self.env,
                                              exec_async=True))
        time.sleep(self.config['sleep'])
        print('Done sleeping')

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        Exec('finalize_hermes',
             PsshExecInfo(hostfile=self.jarvis.hostfile,
                          env=self.env))
        if self.daemon_node is not None:
            self.daemon_node.wait()
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

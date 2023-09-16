"""
This module provides classes and methods to launch the HermesRun service.
Labstor is ....
"""

from jarvis_cd.basic.pkg import Service
from jarvis_util import *


class HermesRun(Service):
    """
    This class provides methods to launch the HermesRun service.
    """
    def _init(self):
        """
        Initialize paths
        """
        self.daemon_pkg = None
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
                'name': 'reinit',
                'msg': 'Destroy previous configuration and rebuild',
                'type': bool,
                'default': False
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

    def _configure_server(self):
        rg = self.jarvis.resource_graph
        hosts = self.jarvis.hostfile

        # Begin making hermes_run config
        labstor_server = {
            'work_orchestrator': {
                'max_workers': 4
            },
            'queue_manager': {
                'queue_depth': 256,
                'max_lanes': 16,
                'max_queues': 1024,
                'shm_allocator': 'kScalablePageAllocator',
                'shm_name': 'labstor_shm',
                'shm_size': '0g'
            }
        }

        # Get network Info
        if len(hosts) > 1:
            net_info = rg.find_net_info(shared=True)
        else:
            net_info = rg.find_net_info(hosts, strip_ips=True, shared=False)
        provider = self.config['provider']
        if provider is None:
            opts = net_info['provider'].unique().list()
            order = ['sockets', 'tcp', 'udp', 'verbs', 'ib']
            for opt in order:
                if opt in opts:
                    provider = opt
                    break
            if provider is None:
                provider = opts[0]
        print(f'Provider: {provider}')
        net_info = net_info[lambda r: str(r['provider']) == provider,
                            ['provider', 'domain']]
        if len(net_info) == 0:
            raise Exception(f'Failed to find hermes_run provider {provider}')
        net_info = net_info.rows[0]
        protocol = net_info['provider']
        domain = net_info['domain']
        hostfile_path = self.jarvis.hostfile.path
        if hostfile_path is None:
            hostfile_path = ''
        labstor_server['rpc'] = {
            'host_file': hostfile_path,
            'protocol': protocol,
            'domain': domain,
            'port': self.config['port'],
            'num_threads': 32
        }
        if self.jarvis.hostfile.path is None:
            labstor_server['rpc']['host_names'] = self.jarvis.hostfile.hosts

        # Save hermes_run configurations
        labstor_server_yaml = f'{self.shared_dir}/labstor_server.yaml'
        YamlFile(labstor_server_yaml).save(labstor_server)
        self.env['LABSTOR_SERVER_CONF'] = labstor_server_yaml

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        print(self.env['LABSTOR_SERVER_CONF'])
        self.daemon_pkg = Exec('labstor_start_runtime',
                                PsshExecInfo(hostfile=self.jarvis.hostfile,
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
        print('Stopping hermes_run')
        Kill('hermes_run',
             PsshExecInfo(hostfile=self.jarvis.hostfile,
                          env=self.env))
        # Exec('labstor_stop_runtime',
        #      PsshExecInfo(hostfile=self.jarvis.hostfile,
        #                   env=self.env))
        print('Client Exited?')
        if self.daemon_pkg is not None:
            self.daemon_pkg.wait()
        print('Daemon Exited?')

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

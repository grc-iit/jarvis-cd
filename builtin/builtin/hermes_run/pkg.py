"""
This module provides classes and methods to launch the HermesRun service.
hrun is ....
"""

from jarvis_cd.basic.pkg import Service, Color
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
                'name': 'data_shm',
                'msg': 'Data buffering space',
                'type': str,
                'default': '8g',
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'rdata_shm',
                'msg': 'Runtime data buffering space',
                'type': str,
                'default': '8g',
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'task_shm',
                'msg': 'Task buffering space',
                'type': str,
                'default': '0g',
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'shm_name',
                'msg': 'The base shared-memory name',
                'type': str,
                'default': 'hrun_shm_${USER}',
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'port',
                'msg': 'The port to listen for data on',
                'type': int,
                'default': 8080,
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'provider',
                'msg': 'The libfabric provider type to use (e.g., sockets)',
                'type': str,
                'default': None,
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'domain',
                'msg': 'The libfabric domain to use (e.g., lo)',
                'type': str,
                'default': None,
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'threads',
                'msg': 'the number of rpc threads to create',
                'type': int,
                'default': 32,
                'class': 'communication',
                'rank': 1,
            },
            {
                'name': 'recency_max',
                'msg': 'time before blob is considered stale (sec)',
                'type': float,
                'default': 1,
                'class': 'buffer organizer',
                'rank': 1,
            },
            {
                'name': 'borg_min_cap',
                'msg': 'Capacity percentage before reorganizing can begin',
                'type': float,
                'default': 0,
                'class': 'buffer organizer',
                'rank': 1,
            },
            {
                'name': 'flush_period',
                'msg': 'Period of time to check for flushing (milliseconds)',
                'type': int,
                'default': 5000,
                'class': 'buffer organizer',
                'rank': 1,
            },
            {
                'name': 'qdepth',
                'msg': 'The depth of queues',
                'type': int,
                'default': 100000,
                'class': 'queuing',
                'rank': 1,
            },
            {
                'name': 'pqdepth',
                'msg': 'The depth of the process queue',
                'type': int,
                'default': 48,
                'class': 'queuing',
                'rank': 1,
            },
            {
                'name': 'qlanes',
                'msg': 'The number of lanes per queue',
                'type': int,
                'default': 4,
                'class': 'queuing',
                'rank': 1,
            },
            {
                'name': 'dworkers',
                'msg': 'The number of core-dedicated workers',
                'type': int,
                'default': 2,
                'class': 'queuing',
                'rank': 1,
            },
            {
                'name': 'oworkers',
                'msg': 'The number of overlapping workers',
                'type': int,
                'default': 4,
                'class': 'queuing',
                'rank': 1,
            },
            {
                'name': 'oworkers_per_core',
                'msg': 'Overlapping workers per core',
                'type': int,
                'default': 32,
                'class': 'queuing',
                'rank': 1,
            },
            {
                'name': 'include',
                'msg': 'Specify paths to include',
                'type': list,
                'default': [],
                'class': 'adapter',
                'rank': 1,
                'args': [
                    {
                        'name': 'path',
                        'msg': 'The path to be included',
                        'type': str
                    },
                ],
                'aliases': ['i']
            },
            {
                'name': 'exclude',
                'msg': 'Specify paths to exclude',
                'type': list,
                'default': [],
                'class': 'adapter',
                'rank': 1,
                'args': [
                    {
                        'name': 'path',
                        'msg': 'The path to be excluded',
                        'type': str
                    },
                ],
                'aliases': ['e']
            },
            {
                'name': 'adapter_mode',
                'msg': 'The adapter mode to use for Hermes',
                'type': str,
                'default': 'default',
                'choices': ['default', 'scratch', 'bypass'],
                'class': 'adapter',
                'rank': 1,
            },
            {
                'name': 'flush_mode',
                'msg': 'The flushing mode to use for adapters',
                'type': str,
                'default': 'async',
                'choices': ['sync', 'async'],
                'class': 'adapter',
                'rank': 1,
            },
            {
                'name': 'page_size',
                'msg': 'The page size to use for adapters',
                'type': str,
                'default': '1m',
                'class': 'adapter',
                'rank': 1,
            },
            {
                'name': 'ram',
                'msg': 'Amount of RAM to use for buffering',
                'type': str,
                'default': '0',
                'class': 'dpe',
                'rank': 1,
            },
            {
                'name': 'dpe',
                'msg': 'The DPE to use by default',
                'type': str,
                'default': 'MinimizeIoTime',
                'class': 'dpe',
                'rank': 1,
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
                ],
                'class': 'dpe',
                'rank': 1,
            }
        ]

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param config: The human-readable jarvis YAML configuration for the
        application.
        :return: None
        """
        self._configure_server()

    def _configure_server(self):
        rg = self.jarvis.resource_graph
        hosts = self.jarvis.hostfile

        # Begin making hermes_run config
        hermes_server = {
            'work_orchestrator': {
                'max_dworkers': self.config['dworkers'],
                'max_oworkers': self.config['oworkers'],
                'oworkers_per_core': self.config['oworkers_per_core'],
            },
            'queue_manager': {
                'queue_depth': self.config['qdepth'],
                'proc_queue_depth': self.config['pqdepth'],
                'max_lanes': self.config['qlanes'],
                'max_queues': 1024,
                'shm_allocator': 'kScalablePageAllocator',
                'shm_name': self.config['shm_name'],
                'shm_size': self.config['task_shm'],
                'data_shm_size': self.config['data_shm'],
                'rdata_shm_size': self.config['rdata_shm'],
            },
            'devices': {},
            'rpc': {}
        }

        # Begin making Hermes client config
        hermes_client = {
            'path_inclusions': ['/tmp/test_hermes'],
            'path_exclusions': ['/'],
            'file_page_size': self.config['page_size']
        }
        if self.config['flush_mode'] == 'async':
            hermes_client['flushing_mode'] = 'kAsync'
        elif self.config['flush_mode'] == 'sync':
            hermes_client['flushing_mode'] = 'kSync'
        if self.config['include'] is not None:
            hermes_client['path_inclusions'] += self.config['include']
        if self.config['exclude'] is not None:
            hermes_client['path_exclusions'] += self.config['exclude']

        # Get storage info
        if len(self.config['devices']) == 0:
            # Get all the fastest storage device mount points on machine
            dev_df = rg.find_storage()
        else:
            # Get the storage devices for the user
            dev_list = [rg.find_storage(dev_types=dev_type,
                                        count_per_pkg=count)
                        for dev_type, count in self.config['devices']]
            dev_df = sdf.concat(dev_list)
        if len(dev_df) == 0:
            raise Exception('Hermes needs at least one storage device')
        devs = dev_df.rows
        self.config['borg_paths'] = []
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
            self.config['borg_paths'].append(mount)
            Mkdir(mount, PsshExecInfo(hostfile=self.jarvis.hostfile,
                                      env=self.env))
        if 'ram' in self.config and self.config['ram'] != '0':
            hermes_server['devices']['ram'] = {
                'mount_point': '',
                'capacity': self.config['ram'],
                'block_size': '4kb',
                'bandwidth': '40GBps',
                'latency': '100ns',
                'is_shared_device': False,
                'borg_capacity_thresh': [self.config['borg_min_cap'], 1.0],
                'slab_sizes': ['256', '512', '1KB',
                               '4KB', '16KB', '64KB', '1MB']
            }

        # Get network Info
        if len(hosts) > 1:
            net_info = rg.find_net_info(hosts, strip_ips=True, shared=True)
        else:
            # net_info = rg.find_net_info(hosts, strip_ips=True)
            net_info = rg.find_net_info(hosts)
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
        self.log(f'Provider: {provider}')
        net_info_save = net_info
        net_info = net_info[lambda r: str(r['provider']) == provider,
                            ['provider', 'domain']]
        if len(net_info) == 0:
            self.log(net_info_save)
            raise Exception(f'Failed to find hermes_run provider {provider}')
        net_info = net_info.rows[0]
        protocol = net_info['provider']
        domain = net_info['domain']
        hostfile_path = self.jarvis.hostfile.path
        if hostfile_path is None:
            hostfile_path = ''
            domain = ''
        hermes_server['rpc'] = {
            'host_file': hostfile_path,
            'protocol': protocol,
            'domain': domain,
            'port': self.config['port'],
            'num_threads': self.config['threads']
        }
        hermes_server['buffer_organizer'] = {
            'recency_max': self.config['recency_max'],
            'flush_period': self.config['flush_period']
        }
        if self.jarvis.hostfile.path is None:
            hermes_server['rpc']['host_names'] = self.jarvis.hostfile.hosts
        hermes_server['default_placement_policy'] = self.config['dpe']
        if self.config['adapter_mode'] == 'default':
            adapter_mode = 'kDefault'
        elif self.config['adapter_mode'] == 'scratch':
            adapter_mode = 'kScratch'
        elif self.config['adapter_mode'] == 'bypass':
            adapter_mode = 'kBypass'
        self.env['HERMES_ADAPTER_MODE'] = adapter_mode
        hermes_server['default_placement_policy'] = self.config['dpe']

        # Save hermes configurations
        hermes_server_yaml = f'{self.shared_dir}/hermes_server.yaml'
        YamlFile(hermes_server_yaml).save(hermes_server)
        self.env['HERMES_CONF'] = hermes_server_yaml

        # Save Hermes client configurations
        hermes_client_yaml = f'{self.shared_dir}/hermes_client.yaml'
        YamlFile(hermes_client_yaml).save(hermes_client)
        self.env['HERMES_CLIENT_CONF'] = hermes_client_yaml

    def start(self):
        """
        Launch an application. E.g., OrangeFS will launch the servers, clients,
        and metadata services on all necessary pkgs.

        :return: None
        """
        self.log(self.env['HERMES_CONF'])
        self.log(self.env['HERMES_CLIENT_CONF'])
        self.daemon_pkg = Exec('hrun_start_runtime',
                                PsshExecInfo(hostfile=self.jarvis.hostfile,
                                             env=self.mod_env,
                                             exec_async=True,
                                             do_dbg=self.config['do_dbg'],
                                             dbg_port=self.config['dbg_port'],
                                             hide_output=self.config['hide_output'],
                                             pipe_stdout=self.config['stdout'],
                                             pipe_stderr=self.config['stderr']))
        time.sleep(self.config['sleep'])
        self.log('Done sleeping')

    def stop(self):
        """
        Stop a running application. E.g., OrangeFS will terminate the servers,
        clients, and metadata services.

        :return: None
        """
        self.log('Stopping hermes_run')
        Exec('hrun_stop_runtime',
             LocalExecInfo(hostfile=self.jarvis.hostfile,
                           env=self.env,
                           exec_async=False,
                           do_dbg=self.config['do_dbg'],
                           dbg_port=self.config['dbg_port'],
                           hide_output=self.config['hide_output']))
        self.log('Client Exited?')
        if self.daemon_pkg is not None:
            self.daemon_pkg.wait()
        self.log('Daemon Exited?')

    def kill(self):
        Kill('hrun',
             PsshExecInfo(hostfile=self.jarvis.hostfile,
                          env=self.env))
        if self.config['do_dbg']:
            Kill('gdbserver',
                 PsshExecInfo(hostfile=self.jarvis.hostfile,
                              env=self.env))
        self.log('Client Exited?')
        if self.daemon_pkg is not None:
            self.daemon_pkg.wait()
        self.log('Daemon Exited?')

    def clean(self):
        """
        Destroy all data for an application. E.g., OrangeFS will delete all
        metadata and data directories in addition to the orangefs.xml file.

        :return: None
        """
        for path in self.config['borg_paths']:
            self.log(f'Removing {path}', Color.YELLOW)
            Rm(path, PsshExecInfo(hostfile=self.jarvis.hostfile))

    def status(self):
        """
        Check whether or not an application is running. E.g., are OrangeFS
        servers running?

        :return: True or false
        """
        return True

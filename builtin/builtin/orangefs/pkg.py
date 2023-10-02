from jarvis_cd.basic.pkg import Service
from jarvis_util import *
from .custom_kern import OrangefsCustomKern
from .ares import OrangefsAres
import os
import time


class Orangefs(Service, OrangefsCustomKern, OrangefsAres):
    def _init(self):
        """
        Initialize paths
        """

    def _configure_menu(self):
        return [
            {
                'name': 'port',
                'msg': 'The port to listen for data on',
                'type': int,
                'default': 3334
            },
            {
                'name': 'dev_type',
                'msg': 'The device to spawn orangefs over',
                'type': str,
                'default': None,
            },
            {
                'name': 'stripe_size',
                'msg': 'The stripe size',
                'type': int,
                'default': 65536,
            },
            {
                'name': 'stripe_dist',
                'msg': 'The striping distribution algorithm',
                'type': str,
                'default': 'simple_stripe',
            },
            {
                'name': 'protocol',
                'msg': 'The network protocol (tcp/ib)',
                'type': str,
                'default': 'tcp',
                'choices': ['tcp', 'ib']
            },
            {
                'name': 'mount',
                'msg': 'Where to mount orangefs clients',
                'type': str,
                'default': None,
            },
            {
                'name': 'md_hosts',
                'msg': 'The number of metadata management servers to spawn',
                'type': int,
                'default': None,
            },
            {
                'name': 'name',
                'msg': 'The name of the orangefs installation',
                'type': int,
                'default': 'orangefs',
            },
            {
                'name': 'sudoenv',
                'msg': 'Whether environment forwarding is supported for sudo',
                'type': bool,
                'default': True,
            },
            {
                'name': 'ares',
                'msg': 'Whether we are using the orangefs on Ares',
                'type': bool,
                'default': False,
            },
        ]

    def configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        self.update_config(kwargs, rebuild=False)
        rg = self.jarvis.resource_graph

        # Configure and save hosts
        self.client_hosts = self.jarvis.hostfile
        self.server_hosts = self.jarvis.hostfile
        self.md_hosts = self.jarvis.hostfile
        if self.config['md_hosts'] is None:
            self.md_hosts = self.server_hosts
        else:
            self.md_hosts = self.md_hosts.subset(self.config['md_hosts'])
        self.config['client_host_set'] = self.client_hosts.hosts
        self.config['server_host_set'] = self.server_hosts.hosts
        self.config['md_host_set'] = self.md_hosts.hosts
        self.config['client_hosts_path'] = f'{self.private_dir}/client_hosts'
        self.config['server_hosts_path'] = f'{self.private_dir}/server_hosts'
        self.config['metadata_hosts_path'] = f'{self.private_dir}/metadata_hosts'
        self.client_hosts.save(self.config['client_hosts_path'])
        self.server_hosts.save(self.config['server_hosts_path'])
        self.md_hosts.save(self.config['metadata_hosts_path'])
        Pscp([self.config['client_hosts_path'],
              self.config['server_hosts_path'],
              self.config['metadata_hosts_path']],
             PsshExecInfo(hosts=self.jarvis.hostfile, env=self.env))

        # Locate storage hardware
        dev_df = []
        if self.config['dev_type'] is None:
            dev_types = ['hdd', 'ssd', 'nvme', 'dimm']
            for dev_type in dev_types:
                dev_df = rg.find_storage(dev_types=[dev_type],
                                         shared=False)
                if len(dev_df) != 0:
                    break
        else:
            dev_df = rg.find_storage(dev_types=[self.config['dev_type']],
                                     shared=False)
        if len(dev_df) == 0:
            raise Exception('Could not find any storage devices :(')
        storage_dir = os.path.expandvars(dev_df.rows[0]['mount'])

        # Define paths
        self.config['pfs_conf'] = f'{self.private_dir}/orangefs.xml'
        self.config['pvfs2tab'] = f'{self.private_dir}/pvfs2tab'
        if self.config['mount'] is None:
            self.config['mount'] = f'{self.private_dir}/client'
        self.config['storage'] = f'{storage_dir}/orangefs_storage'
        self.config['metadata'] = f'{storage_dir}/orangefs_metadata'
        self.config['log'] = f'{self.private_dir}/orangefs_server.log'
        self.config['client_log'] = f'{self.private_dir}/orangefs_client.log'

        # generate PFS Gen config
        if self.config['protocol'] == 'tcp':
            proto_cmd = f'--tcpport {self.config["port"]}'
        elif self.config['protocol'] == 'ib':
            proto_cmd = f'--ibport {self.config["port"]}'
        else:
            raise Exception("Protocol must be either tcp or ib")
        pvfs_gen_cmd = [
            'pvfs2-genconfig',
            '--quiet',
            f'--protocol {self.config["protocol"]}',
            proto_cmd,
            f'--dist-name {self.config["stripe_dist"]}',
            f'--dist-params \"strip_size: {self.config["stripe_size"]}\"',
            f'--ioservers {self.server_hosts.ip_str(sep=",")}',
            f'--metaservers {self.md_hosts.ip_str(sep=",")}',
            f'--storage {self.config["storage"]}',
            f'--metadata {self.config["metadata"]}',
            f'--logfile {self.config["log"]}',
            f'--fsname {self.config["name"]}',
            self.config['pfs_conf']
        ]
        pvfs_gen_cmd = " ".join(pvfs_gen_cmd)
        Exec(pvfs_gen_cmd, LocalExecInfo(env=self.env))
        Pscp(self.config['pfs_conf'],
             PsshExecInfo(hosts=self.jarvis.hostfile, env=self.env))

        # Create storage directories
        Mkdir(self.config['mount'],
              PsshExecInfo(hosts=self.client_hosts, env=self.env))
        Mkdir(self.config['storage'], PsshExecInfo(hosts=self.server_hosts,
                                                   env=self.env))
        Mkdir(self.config['metadata'], PsshExecInfo(hosts=self.md_hosts,
                                                    env=self.env))

        # Set pvfstab on clients
        for i, client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.list()[
                i % len(self.md_hosts)].hosts_ip[0]
            pvfs2tab_tmp = f'{self.private_dir}/pvfs2tab_{i}'
            with open(pvfs2tab_tmp) as fp:
                fp.write('{protocol}://{ip}:{port}/{name} {mount_point} pvfs2 defaults,auto 0 0'.format(
                    protocol=self.config['protocol'],
                    port=self.config['port'],
                    ip=metadata_server_ip,
                    name=self.config['name'],
                    mount_point=self.config['mount'],
                    client_pvfs2tab=self.config['pvfs2tab']))
            Scp([(pvfs2tab_tmp, self.config['pvfs2tab'])],
                SshExecInfo(hosts=client))
            print(f'Creating pvfs2tab on {client}')
        self.env['PVFS2TAB_FILE'] = self.config['pvfs2tab']

        # Initialize servers
        if not self.config['ares']:
            for host in self.server_hosts.list():
                host_ip = host.hosts_ip[0]
                server_start_cmds = [
                    f'pvfs2-server -f {self.config["pfs_conf"]} -a {host_ip}',
                ]
                Exec(server_start_cmds, SshExecInfo(
                    hosts=host,
                    env=self.env))

    def _load_config(self):
        if 'sudoenv' not in self.config:
            self.config['sudoenv'] = True
        self.client_hosts = Hostfile(all_hosts=self.config['client_host_set'])
        self.server_hosts = Hostfile(all_hosts=self.config['server_host_set'])
        self.md_hosts = Hostfile(all_hosts=self.config['md_host_set'])
        self.ofs_path = self.env['ORANGEFS_PATH']

    def start(self):
        self._load_config()
        # start pfs servers
        if self.config['ares']:
            self.ares_start()
        else:
            self.custom_start()

    def stop(self):
        self._load_config()
        if self.config['ares']:
            self.ares_stop()
        else:
            self.custom_stop()

    def clean(self):
        self._load_config()

        Rm(self.config['mount'],
           PsshExecInfo(hosts=self.client_hosts,
                        env=self.env))
        Rm(self.config['storage'],
           PsshExecInfo(hosts=self.server_hosts,
                        env=self.env))
        Rm(self.config['metadata'],
           PsshExecInfo(hosts=self.md_hosts,
                        env=self.env))

    def status(self):
        self._load_config()
        Exec('mount | grep pvfs',
             PsshExecInfo(hosts=self.server_hosts,
                          env=self.env))
        verify_server_cmd = [
            f'pvfs2-ping -m {self.config["mount"]} | grep \"appears to be correctly configured\"'
        ]
        Exec(verify_server_cmd,
             PsshExecInfo(hosts=self.client_hosts,
                          env=self.env))
        return True

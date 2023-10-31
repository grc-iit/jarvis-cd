from jarvis_cd.basic.pkg import Service
from jarvis_util import *
import os


class OrangefsFuse(Service):
    def _init(self):
        """
        Initialize paths
        """
        self.pfs_conf = None

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
                'msg': 'The device to spawn orangefs over',
                'type': int,
                'default': 65536,
            },
            {
                'name': 'stripe_dist',
                'msg': 'simple_stripe',
                'type': int,
                'default': None,
            },
            {
                'name': 'protocol',
                'msg': 'The device to spawn orangefs over',
                'type': int,
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
        ]

    def default_configure(self):
        config = {
            'server': {
                'pvfs2_protocol': 'tcp',
                'pvfs2_port': 3334,
                'distribution_name': 'simple_stripe',
                'stripe_size': 65536,
                'storage_dir': 'orangefs_data/data',
                'log': f'{self.private_dir}/orangefs_server.log'
            },
            'client': {
                'pvfs2tab': f'{self.private_dir}/pvfs2tab',
                'mountpoint': f'{self.private_dir}/orangefs_data/client'
            },
            'metadata': {
                f'{self.config_dir}/orangefs_data/metadata'
            }
        }
        return config

    def _configure(self, **kwargs):
        """
        Converts the Jarvis configuration to application-specific configuration.
        E.g., OrangeFS produces an orangefs.xml file.

        :param kwargs: Configuration parameters for this pkg.
        :return: None
        """
        rg = self.jarvis.resource_graph
        self.md_hosts = self.jarvis.hostfile
        if self.config['md_hosts'] is None:
            count = int(len(self.md_hosts) / 4)
            if count < 1:
                count = 1
            self.md_hosts = self.md_hosts.subset(count)
        else:
            self.md_hosts = self.md_hosts.subset('md_hosts')
        self.client_hosts = self.jarvis.hostfile
        self.server_hosts = self.jarvis.hostfile

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

        # Define paths
        self.config['pfs_conf'] = f'{self.private_dir}/orangefs.xml'
        self.config['pvfs2tab'] = f'{self.private_dir}/pvfs2tab'
        if self.config['mount'] is None:
            self.config['mount'] = f'{self.private_dir}/client'
        self.config['storage'] = f'{dev_df["mount"]}/orangefs_storage'
        self.config['metadata'] = f'{dev_df["mount"]}/orangefs_metadata'
        self.config['log'] = f'{self.private_dir}/orangefs_server.log'
        
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
            f'--dist-name {self.config["stripe_dist"]}'
            f'--dist-params strip_size: {self.config["stripe_size"]}'
            f'--ioservers {self.server_hosts.ip_str(sep=",")}',
            f'--metaservers {self.md_hosts.ip_str(sep=",")}',
            f'--storage {self.config["storage"]}',
            f'--metadata {self.config["metadata"]}',
            f'--logfile {self.config["log"]}',
            self.pfs_conf
        ]
        pvfs_gen_cmd = " ".join(pvfs_gen_cmd)
        Exec(pvfs_gen_cmd)
        Pscp(self.pfs_conf, SshExecInfo(hostfile=self.config['hostfile']))

        # Create storage directories
        Mkdir(self.config['mount'], hosts=self.client_hosts)
        Mkdir(self.config['storage'], hosts=self.server_hosts)
        Mkdir(self.config['metadata'], hosts=self.md_hosts)

        # Set pvfstab on clients
        for i, client in self.client_hosts.enumerate():
            mdm_ip = self.md_hosts.hostname_list()[i % len(self.md_hosts)]
            cmd = 'echo "{protocol}://{ip}:{port}/pfs {mount_point} pvfs2 defaults,auto 0 0" > {client_pvfs2tab}'.format(
                protocol=self.config['protocol'],
                port=self.config['port'],
                ip=mdm_ip,
                mount_point=self.config['mount'],
                client_pvfs2tab=self.config['pvfs2tab']
            )
            Exec(cmd, SshExecInfo(hosts=client))

    def start(self):
        # start pfs servers
        for host in self.server_hosts:
            # pvfs2_server = os.path.join(self.orangefs_root,"sbin","pvfs2-server")
            server_start_cmds = [
                f"pvfs2-server {self.pfs_conf} -f -a {host}",
                f"pvfs2-server {self.pfs_conf} -a {host}"
            ]
            Exec(server_start_cmds, hosts=host)
        Sleep(5)
        self.Status()

        # start pfs client
        # pvfs2_fuse = os.path.join(self.orangefs_root, "bin", "pvfs2fuse")
        for i,client in self.client_hosts.enumerate():
            mdm_ip = self.md_hosts.hostname_list()[i % len(self.md_hosts)]
            start_client_cmds = [
                "pvfs2fuse -o fs_spec={protocol}://{ip}:{port}/pfs {mount_point}".format(
                    pvfs2_fuse=pvfs2_fuse,
                    protocol=self.config['protocol'],
                    port=self.config['port'],
                    ip=mdm_ip,
                    mount_point=self.config['mount'])
            ]
            Exec(start_client_cmds, hosts=client)

    def stop(self):
        cmds = [
            f"umount -l {self.config['mount']}",
            f"umount -f {self.config['mount']}",
            f"umount {self.config['mount']}",
            f"killall -9 pvfs2-client",
            f"killall -9 pvfs2-client-core"
        ]
        Exec(cmds, hosts=self.client_hosts)
        Exec("killall -9 pvfs2-server", hosts=self.server_hosts)
        Exec("pgrep -la pvfs2-server", hosts=self.client_hosts)

    def clean(self):
        Rm(self.config['mount'], hosts=self.client_hosts)
        Rm(self.config['storage'], hosts=self.server_hosts)
        Rm(self.config['metadata'], hosts=self.md_hosts)

    def status(self):
        Exec("mount | grep pvfs", hosts=self.server_hosts)
        verify_server_cmd = [
            f"export PVFS2TAB_FILE={self.config['pvfs2tab']}",
            f"pvfs2-ping -m {self.config['mount']} | grep 'appears to be correctly configured'"
        ]
        verify_server_cmd = ';'.join(verify_server_cmd)
        Exec(verify_server_cmd, hosts=self.client_hosts)
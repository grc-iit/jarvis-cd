from jarvis_cd.basic.node import Service
from jarvis_util import *
import os


class Orangefs(Service):
    def __init__(self):
        """
        Initialize paths
        """
        super().__init__()
        self.pfs_conf = None

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

    def configure(self, **kwargs):
        self.default_configure()
        self.config.update(config)
        self.pfs_conf = f'{self.private_dir}/orangefs.xml'
        
        # generate PFS Gen config
        if self.config['server']['pvfs2_protocol'] == 'tcp':
            proto_cmd = f'--tcpport {self.config["server"]["pvfs2_port"]}'
        elif self.config['server']['pvfs2_protocol'] == 'ib':
            proto_cmd = f'--ibport {self.config["server"]["pvfs2_port"]}'
        else:
            raise Exception("Protocol must be either tcp or ib")
        pvfs_gen_cmd = [
            'pvfs2-genconfig',
            '--quiet',
            f'--protocol {self.config["server"]["pvfs2_protocol"]}',
            proto_cmd,
            f'--dist-name {self.config["server"]["distribution_name"]}'
            f'--dist-params strip_size: {self.config["server"]["STRIPE_SIZE"]}'
            f'--ioservers {self.server_hosts.ip_str(sep=",")}',
            f'--metaservers {self.md_hosts.ip_str(sep=",")}',
            f'--storage {self.config["server"]["storage_dir"]}',
            f'--metadata {self.config["metadata"]["meta_dir"]}',
            f'--logfile {self.config["server"]["log"]}',
            self.pfs_conf
        ]
        pvfs_gen_cmd = " ".join(pvfs_gen_cmd)
        Exec(pvfs_gen_cmd)
        Pscp(self.pfs_conf, ExecInfo(hostfile=self.config['hostfile']))

        #Create storage directories
        Mkdir(self.config['client']['mount_point'], hosts=self.client_hosts)
        Mkdir(self.config['server']['storage_dir'], hosts=self.server_hosts)
        Mkdir(self.config['metadata']['meta_dir'], hosts=self.md_hosts)

        #set pvfstab on clients
        for i, client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.hostname_list()[i % len(self.md_hosts)]
            cmd = "echo '{protocol}://{ip}:{port}/pfs {mount_point} pvfs2 defaults,auto 0 0' > {client_pvfs2tab}".format(
                protocol=self.config['server']['pvfs2_protocol'],
                port=self.config['server']['pvfs2_port'],
                ip=metadata_server_ip,
                mount_point=self.config['client']['mount_point'],
                client_pvfs2tab=self.config['client']['PVFS2TAB']
            )
            Exec(cmd, SshExecInfo(hosts=client))

    def start(self):
        # start pfs servers
        for host in self.server_hosts:
            pvfs2_server = os.path.join(self.orangefs_root,"sbin","pvfs2-server")
            server_start_cmds = [
                f"{pvfs2_server} {self.pfs_conf} -f -a {host}",
                f"{pvfs2_server} {self.pfs_conf} -a {host}"
            ]
            Exec(server_start_cmds, hosts=host)
        Sleep(5)
        self.Status()

        # start pfs client
        pvfs2_fuse = os.path.join(self.orangefs_root, "bin", "pvfs2fuse")
        for i,client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.hostname_list()[i % len(self.md_hosts)]
            start_client_cmds = [
                "{pvfs2_fuse} -o fs_spec={protocol}://{ip}:{port}/pfs {mount_point}".format(
                    pvfs2_fuse=pvfs2_fuse,
                    protocol=self.config['server']['pvfs2_protocol'],
                    port=self.config['server']['pvfs2_port'],
                    ip=metadata_server_ip,
                    mount_point=self.config['client']['mount_point'])
            ]
            Exec(start_client_cmds, hosts=client)

    def stop(self):
        cmds = [
            f"umount -l {self.config['client']['mount_point']}",
            f"umount -f {self.config['client']['mount_point']}",
            f"umount {self.config['client']['mount_point']}",
            f"killall -9 pvfs2-client",
            f"killall -9 pvfs2-client-core"
        ]
        Exec(cmds, hosts=self.client_hosts, sudo=True)
        Exec("killall -9 pvfs2-server", sudo=True, hosts=self.server_hosts)
        Exec("pgrep -la pvfs2-server", hosts=self.client_hosts)

    def clean(self):
        Rm(self.config['client']['mount_point'], hosts=self.client_hosts)
        Rm(self.config['server']['storage_dir'], hosts=self.server_hosts)
        Rm(self.config['metadata']['meta_dir'], hosts=self.md_hosts)

    def status(self):
        Exec("mount | grep pvfs", hosts=self.server_hosts)
        verify_server_cmd = [
            f"export PVFS2TAB_FILE={self.config['client']['PVFS2TAB']}",
            f"pvfs2-ping -m {self.config['client']['mount_point']} | grep 'appears to be correctly configured'"
        ]
        verify_server_cmd = ';'.join(verify_server_cmd)
        Exec(verify_server_cmd, hosts=self.client_hosts)
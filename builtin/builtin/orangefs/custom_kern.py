from jarvis_util import *


class OrangefsCustomKern:
    def custom_start(self):
        # start pfs servers
        print("Starting the PFS servers")
        for host in self.server_hosts.list():
            host_ip = host.hosts_ip[0]
            server_start_cmds = [
                f'pvfs2-server {self.config["pfs_conf"]} -a {host_ip}'
            ]
            Exec(server_start_cmds, SshExecInfo(
                hosts=host,
                env=self.env))
        time.sleep(5)
        self.status()

        # insert OFS kernel module
        print("Inserting OrangeFS kernel module")
        Exec('modprobe orangefs', PsshExecInfo(sudo=True,
                                               sudoenv=self.config['sudoenv'],
                                               hosts=self.client_hosts,
                                               env=self.env))

        # start pfs client
        print("Starting the OrangeFS clients")
        for i, client in self.client_hosts.enumerate():
            metadata_server_ip = self.md_hosts.list()[
                i % len(self.md_hosts)].hosts_ip[0]
            start_client_cmd = f'{self.ofs_path}/sbin/pvfs2-client -p {self.ofs_path}/sbin/pvfs2-client-core -L {self.config["client_log"]}'
            mount_client = 'mount -t pvfs2 {protocol}://{ip}:{port}/{name} {mount_point}'.format(
                protocol=self.config['protocol'],
                port=self.config['port'],
                ip=metadata_server_ip,
                name=self.config['name'],
                mount_point=self.config['mount'])
            cmds = [start_client_cmd, mount_client]
            Exec(cmds, SshExecInfo(
                hosts=client,
                env=self.env,
                sudo=True,
                sudoenv=self.config['sudoenv']))

    def custom_stop(self):
        cmds = [
            f'umount -l {self.config["mount"]}',
            f'umount -f {self.config["mount"]}',
            f'umount {self.config["mount"]}',
        ]
        Exec(cmds, PsshExecInfo(hosts=self.client_hosts,
                                env=self.env,
                                sudo=True,
                                sudoenv=self.config['sudoenv']))
        cmds = [
            f'killall -9 pvfs2-client',
            f'killall -9 pvfs2-client-core'
        ]
        Exec(cmds, PsshExecInfo(hosts=self.client_hosts,
                                env=self.env))
        Exec('killall -9 pvfs2-server',
             PsshExecInfo(hosts=self.server_hosts,
                          env=self.env))
        Exec('pgrep -la pvfs2-server',
             PsshExecInfo(hosts=self.client_hosts,
                          env=self.env))

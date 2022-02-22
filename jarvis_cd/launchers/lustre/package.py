from jarvis_cd.echo_node import EchoNode
from jarvis_cd.exec_node import ExecNode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.launcher import Launcher, LauncherConfig
import os
import socket

from jarvis_cd.scp_node import SCPNode
from jarvis_cd.sleep_node import SleepNode
from jarvis_cd.ssh_node import SSHNode

class Lustre(Launcher):
    def __init__(self, config_path=None, args=None):
        super().__init__('lustre', config_path, args)

    def _LoadConfig(self):
        self.ssh_port = int(self.config['BASIC']['SSH_PORT'])
        self.ssh_user = self.config['BASIC']['SSH_USER']
        self.oss_hosts = Hostfile().LoadHostfile(self.config['OBJECT_STORAGE_SERVERS']['HOSTFILE'])
        self.client_hosts = Hostfile().LoadHostfile(self.config['CLIENT']['HOSTFILE'])
        self.num_ost_per_node = int(self.config['OBJECT_STORAGE_SERVERS']['NUM_OST_PER_NODE'])

    def SetNumHosts(self, num_oss_hosts, num_client_hosts):
        self.oss_hosts.SelectHosts(num_oss_hosts)
        return

    def _DefineClean(self):
        nodes = []
        return nodes

    def _DefineStatus(self):
        nodes = []
        return nodes

    def _DefineStop(self):
        nodes = []
        return nodes

    def _DefineStart(self):
        nodes = []

        #Make and mount Lustre Management Server (MGS)
        make_mgt_cmd = f"mkfs.lustre --reformat --mgs {self.config['MANAGEMENT_SERVER']['STORAGE']}"
        mkdir_mgt_cmd = f"mkdir -p {self.config['MANAGEMENT_SERVER']['MOUNT_POINT']}"
        mount_mgt_cmd = f"mount -t lustre {self.config['MANAGEMENT_SERVER']['STORAGE']} {self.config['MANAGEMENT_SERVER']['MOUNT_POINT']}"
        nodes.append(SSHNode("make_mgt",
                             self.config['MANAGEMENT_SERVER']['HOST'],
                             f'{make_mgt_cmd};{mkdir_mgt_cmd};{mount_mgt_cmd}',
                             username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))

        #Make and mount Lustre Metatadata Server (MDT)
        make_mdt_cmd = (
            f"mkfs.lustre "
            f"--fsname={self.config['BASIC']['FSNAME']} "
            f"--reformat "
            f"--mgsnode={self.config['MANAGEMENT_SERVER']['HOST']}@tcp "
            f"--mdt "
            f"--index=0 {self.config['METADATA_SERVER']['STORAGE']}"
        )
        mkdir_mdt_cmd = f"mkdir -p {self.config['METADATA_SERVER']['MOUNT_POINT']}"
        mount_mdt_cmd = f"mount -t lustre {self.config['METADATA_SERVER']['STORAGE']} {self.config['METADATA_SERVER']['MOUNT_POINT']}"
        nodes.append(SSHNode(
            "make_mdt",
            self.config['METADATA_SERVER']['HOST'],
            f'{make_mdt_cmd};{mkdir_mdt_cmd};{mount_mdt_cmd}',
            username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))

        #Make and mount Lustre Object Storage Server (OSS) and Targets (OSTs)
        index = 1
        for host in self.oss_hosts:
            make_ost_cmd = []
            mkdir_ost_cmd = []
            mount_ost_cmd = []
            for i in range(self.num_ost_per_node):
                ost_id = f"OST{i}"
                ost_dev = f"{self.config['OBJECT_STORAGE_SERVERS'][ost_id]}"
                ost_dir = f"{self.config['OBJECT_STORAGE_SERVERS']['MOUNT_POINT_BASE']}{i}"
                make_ost_cmd.append((
                    f"mkfs.lustre --ost "
                    f"--reformat "
                    f"--fsname={self.config['BASIC']['FSNAME']} "
                    f"--mgsnode={self.config['MANAGEMENT_SERVER']['HOST']}@tcp "
                    f"--index={index} {ost_dev}"
                ))
                mkdir_ost_cmd.append(f"mkdir -p {ost_dir}")
                mount_ost_cmd.append(f"mount -t lustre {ost_dev} {ost_dir}")
                index += 1
            make_ost_cmd = ';'.join(make_ost_cmd)
            mkdir_ost_cmd = ';'.join(mkdir_ost_cmd)
            mount_ost_cmd = ';'.join(mount_ost_cmd)
            nodes.append(SSHNode("mount_ost",
                                 host,
                                 f'{make_ost_cmd};{mkdir_ost_cmd};{mount_ost_cmd}',
                                 username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))

        #Mount the Lustre PFS on the clients
        mkdir_client_cmd = f"mkdir -p {self.config['CLIENT']['MOUNT_POINT']}"
        mount_client_cmd = f"mount -t lustre {self.config['MANAGEMENT_SERVER']['HOST']}@tcp:/{self.config['BASIC']['FSNAME']} {self.config['CLIENT']['MOUNT_POINT']}"
        nodes.append(SSHNode("mount_client",
                             self.client_hosts,
                             f'{mkdir_client_cmd};{mount_client_cmd}',
                             username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))
        return nodes





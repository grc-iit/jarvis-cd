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
        self.oss_hosts = Hostfile().LoadHostfile(self.config['OBJECT_STORAGE_SERVERS']['HOSTFILE'])

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
        make_mgt_cmd = f"sudo mkfs.lustre --reformat --mgs {self.config['MANAGEMENT_SERVER']['STORAGE']}"
        mkdir_mgt_cmd = f"mkdir {self.config['MANAGEMENT_SERVER']['MOUNT_POINT']}"
        mount_mgt_cmd = f"sudo mount -t lustre {self.config['MANAGEMENT_SERVER']['STORAGE']} {self.config['MANAGEMENT_SERVER']['MOUNT_POINT']}"
        nodes.append(SSHNode("make_mgt", self.config['MANAGEMENT_SERVER']['HOST'], f'{make_mgt_cmd};{mkdir_mgt_cmd};{mount_mgt_cmd}'))

        #Make and mount Lustre Metatadata Server (MDT)
        make_mdt_cmd = (
            f"sudo mkfs.lustre "
            f"--fsname={self.config['BASIC']['FSNAME']} "
            f"--reformat "
            f"--replace "
            f"--mgsnode={self.config['MANAGEMENT_SERVER']['HOST']}@tcp "
            f"--mdt"
            f"--index=0 {self.config['METADATA_SERVER']['STORAGE']}"
        )
        mkdir_mdt_cmd = f"mkdir {self.config['METADATA_SERVER']['MOUNT_POINT']}"
        mount_mdt_cmd = f"sudo mount -t lustre {self.config['METADATA_SERVER']['STORAGE']} {self.config['METADATA_SERVER']['MOUNT_POINT']}"
        nodes.append(SSHNode("make_mdt", self.config['METADATA_SERVER']['HOST'], f'{make_mdt_cmd};{mkdir_mdt_cmd};{mount_mdt_cmd}'))

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
                    f"sudo mkfs.lustre --ost "
                    f"--fsname={self.config['BASIC']['FSNAME']} "
                    f"--mgsnode={self.config['MANAGEMENT_SERVER']['HOST']}@tcp "
                    f"--index={index} {ost_dev}"
                ))
                mkdir_ost_cmd.append(f"mkdir {ost_dir}")
                mount_ost_cmd.append(f"sudo mount -t lustre {ost_dir} {ost_dev}")
                index += 1
            make_ost_cmd = ';'.join(make_ost_cmd)
            mkdir_ost_cmd = ';'.join(mkdir_ost_cmd)
            mount_ost_cmd = ';'.join(mount_ost_cmd)
            nodes.append("mount_ost", host, f'{make_ost_cmd};{mkdir_ost_cmd};{mount_ost_cmd}')
        return nodes





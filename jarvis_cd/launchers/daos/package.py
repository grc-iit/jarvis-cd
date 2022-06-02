from jarvis_cd.echo_node import EchoNode
from jarvis_cd.exec_node import ExecNode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.launcher import Launcher, LauncherConfig
import os
import socket
import yaml

from jarvis_cd.scp_node import SCPNode
from jarvis_cd.sleep_node import SleepNode
from jarvis_cd.ssh_node import SSHNode

class Daos(Launcher):
    def __init__(self, config_path=None, args=None):
        super().__init__('daos', config_path, args)

    def _ProcessConfig(self):
        return

    def _DefineClean(self):
        return nodes

    def _DefineStatus(self):
        nodes = []
        return nodes

    def _DefineStop(self):
        nodes = []
        return nodes

    def _DefineInit(self):
        nodes = []
        #Generate security certificates
        gen_certificates_cmd = f"{self.config['DAOS_ROOT']}/lib64/daos/certgen/gen_certificates.sh {self.scaffold_dir}"
        nodes.append(ExecNode('Generate Certificates', gen_certificates_cmd))
        #Generate config files
        self._CreateServerConfig()
        self._CreateAgentConfig()
        self._CreateControlConfig()
        #Detect network type (if not already configured)

        return nodes

    def _DefineStart(self):
        nodes = []

        # Make and mount Lustre Management Server (MGS)
        nodes.append(SSHNode("make_mgt",
                             self.config['MANAGEMENT_SERVER']['HOST'],
                             mount_mgt_cmd,
                             username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))

        # Make and mount Lustre Metatadata Server (MDT)
        mount_mdt_cmd = f"mount -t lustre {self.config['METADATA_SERVER']['STORAGE']} {self.config['METADATA_SERVER']['MOUNT_POINT']}"
        nodes.append(SSHNode(
            "make_mdt",
            self.config['METADATA_SERVER']['HOST'],
            mount_mdt_cmd,
            username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))

        # Make and mount Lustre Object Storage Server (OSS) and Targets (OSTs)
        index = 1
        for host in self.oss_hosts:
            mount_ost_cmd = []
            for i, ost_dev in enumerate(self.osts):
                ost_dir = f"{self.config['OBJECT_STORAGE_SERVERS']['MOUNT_POINT_BASE']}{i}"
                mount_ost_cmd.append(f"mount -t lustre {ost_dev} {ost_dir}")
                index += 1
            mount_ost_cmd = ';'.join(mount_ost_cmd)
            nodes.append(SSHNode("mount_ost",
                                 host,
                                 mount_ost_cmd,
                                 username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))

        # Mount the Lustre PFS on the clients
        mount_client_cmd = f"mount -t lustre {self.config['MANAGEMENT_SERVER']['HOST']}@tcp:/{self.config['BASIC']['FSNAME']} {self.config['CLIENT']['MOUNT_POINT']}"
        nodes.append(SSHNode("mount_client",
                             self.client_hosts,
                             mount_client_cmd,
                             username=self.ssh_user, port=self.ssh_port, print_output=True, sudo=True))
        return nodes

    def _CreateServerConfig(self):
        with open(self.config['CONF']['SERVER'], 'w') as fp:
            yaml.dump(self.config['SERVER'], fp)
        return

    def _CreateAgentConfig(self):
        with open(self.config['CONF']['AGENT'], 'w') as fp:
            yaml.dump(self.config['AGENT'], fp)
        return

    def _CreateControlConfig(self):
        with open(self.config['CONF']['CONTROL'], 'w') as fp:
            yaml.dump(self.config['CONTROL'], fp)
        return
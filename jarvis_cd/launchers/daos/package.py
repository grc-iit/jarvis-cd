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
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['DAOS_ROOT']}"
        node.append(ExecNode('Start DAOS', server_start_cmd))
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        return nodes

    def _DefineClean(self):
        return nodes

    def _DefineStop(self):
        nodes = []
        return nodes

    def _DefineStatus(self):
        nodes = []
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
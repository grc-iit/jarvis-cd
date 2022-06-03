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
        self.server_hosts = Hostfile().LoadHostfile(self.config['SERVER']['access_points'])
        self.agent_hosts = Hostfile().LoadHostfile(self.config['AGENT']['access_points'])
        self.control_hosts = Hostfile().LoadHostfile(self.config['CONTROL']['hostlist'])
        return

    def _DefineInit(self):
        nodes = []
        #Generate security certificates
        gen_certificates_cmd = f"{self.config['DAOS_ROOT']}/lib64/daos/certgen/gen_certificates.sh {self.scaffold_dir}"
        nodes.append(ExecNode('Generate Certificates', gen_certificates_cmd))
        #Copy the certificates to all servers
        #nodes.append(SCPNode('Distribute Certificates', ))
        #Generate config files
        self._CreateServerConfig()
        self._CreateAgentConfig()
        self._CreateControlConfig()
        #Start DAOS server
        #server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        #node.append(ExecNode('Start DAOS', server_start_cmd, sudo=True))
        #Format storage
        #storage_format_cmd = f"${DAOS_ROOT}/bin/dmg storage format --force -o {self.config['CONF']['CONTROL']}"
        #node.append(ExecNode('Format DAOS', storage_format_cmd, sudo=True))
        return nodes

    def _DefineStart(self):
        nodes = []
        #Start DAOS server
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        nodes.append(ExecNode('Start DAOS', server_start_cmd, sudo=True))
        #Start client
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        nodes.append(ExecNode('Start DAOS Agent', agent_start_cmd, sudo=True))
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
        self.config['SERVER']['allow_insecure'] = self.config['SECURE']
        self.config['SERVER']['port'] = self.config['PORT']
        self.config['SERVER']['name'] = self.config['NAME']
        self.config['SERVER']['access_points'] = self.server_hosts.list()
        with open(self.config['CONF']['SERVER'], 'w') as fp:
            yaml.dump(self.config['SERVER'], fp)
        return

    def _CreateAgentConfig(self):
        self.config['AGENT']['allow_insecure'] = self.config['SECURE']
        self.config['AGENT']['port'] = self.config['PORT']
        self.config['AGENT']['name'] = self.config['NAME']
        self.config['AGENT']['access_points'] = self.agent_hosts.list()
        with open(self.config['CONF']['AGENT'], 'w') as fp:
            yaml.dump(self.config['AGENT'], fp)
        return

    def _CreateControlConfig(self):
        self.config['CONTROL']['allow_insecure'] = self.config['SECURE']
        self.config['CONTROL']['port'] = self.config['PORT']
        self.config['CONTROL']['name'] = self.config['NAME']
        self.config['CONTROL']['hostlist'] = self.control_hosts.list()
        with open(self.config['CONF']['CONTROL'], 'w') as fp:
            yaml.dump(self.config['CONTROL'], fp)
        return
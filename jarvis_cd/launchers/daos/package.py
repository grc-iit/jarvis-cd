from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.comm.scp_node import SCPNode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.launchers.launcher import Launcher
from jarvis_cd.spack.link_package import LinkSpackage
import yaml

class Daos(Launcher):
    def __init__(self, config_path=None, args=None):
        super().__init__('daos', config_path, args)

    def _ProcessConfig(self):
        self.server_hosts = Hostfile().LoadHostfile(self.config['SERVER']['access_points'])
        self.agent_hosts = Hostfile().LoadHostfile(self.config['AGENT']['access_points'])
        self.control_hosts = Hostfile().LoadHostfile(self.config['CONTROL']['hostlist'])
        return

    def _DefineInit(self):
        #Create DAOS_ROOT sybmolic link
        LinkSpackage("Link Spackage", self.server_hosts, self.config['DAOS_SPACK'], self.config['DAOS_ROOT']).Run()
        LinkSpackage("Link Spackage", self.agent_hosts, self.config['DAOS_SPACK'], self.config['DAOS_ROOT']).Run()
        LinkSpackage("Link Spackage", self.control_hosts, self.config['DAOS_SPACK'], self.config['DAOS_ROOT']).Run()
        #Generate security certificates
        gen_certificates_cmd = f"{self.config['DAOS_ROOT']}/lib64/daos/certgen/gen_certificates.sh {self.scaffold_dir}"
        ExecNode('Generate Certificates', gen_certificates_cmd).Run()
        #Copy the certificates to all servers
        SCPNode('Distribute Certificates', self.server_hosts, f"{self.config['SCAFFOLD']}/daosCA", f"{self.config['SCAFFOLD']}/daosCA")
        SCPNode('Distribute Certificates', self.agent_hosts, f"{self.config['SCAFFOLD']}/daosCA", f"{self.config['SCAFFOLD']}/daosCA")
        SCPNode('Distribute Certificates', self.control_hosts, f"{self.config['SCAFFOLD']}/daosCA", f"{self.config['SCAFFOLD']}/daosCA")
        #Generate config files
        self._CreateServerConfig()
        self._CreateAgentConfig()
        self._CreateControlConfig()
        #Start dummy DAOS server (on all server nodes)
        print("Starting DAOS server")
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        print(server_start_cmd)
        SSHNode('Start DAOS', self.server_hosts, server_start_cmd, sudo=True).Run()
        #Get networking options
        print("Scanning networks")
        network_check_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} network scan > {self.config['SCAFFOLD']}/netscan.txt"
        ExecNode('Get Networks', network_check_cmd, sudo=True, shell=True).Run()
        #Format storage
        print("Formatting DAOS storage")
        storage_format_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg storage format --force -o {self.config['CONF']['CONTROL']}"
        ExecNode('Format DAOS', storage_format_cmd, sudo=True).Run()
        #Create storage pools
        print("Create storage pools")
        for pool in self.config['POOLS']:
            create_pool_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} pool create -z {pool['size']} --label {pool['label']}"
            ExecNode('Create Pool', create_pool_cmd, sudo=True).Run()
        #Create containers
        print("Create containers")
        for container in self.config['CONTAINERS']:
            create_container_cmd = [
                f"{self.config['DAOS_ROOT']}/bin/daos container create",
                f"--type {container['type']}",
                f"--pool {container['pool']}",
                f"--label {container['label']}"
            ]
            create_container_cmd = " ".join(create_container_cmd)
            ExecNode('Create Container', create_container_cmd, sudo=True).Run()

    def _DefineStart(self):
        nodes = []
        #Start DAOS server
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        nodes.append(ExecNode('Start DAOS', server_start_cmd, sudo=True))
        #Start client
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        nodes.append(ExecNode('Start DAOS Agent', agent_start_cmd, sudo=True))
        #Mount containers on clients
        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                mount_cmd = [
                    f"{self.config['DAOS_ROOT']}/bin/dfuse",
                    f"--pool {container['pool']}",
                    f"--container {container['label']}",
                    f"-m {container['mount']}"
                ]
                mount_cmd = " ".join(mount_cmd)
                SSHNode('Mount Container', self.agent_hosts, mount_cmd).Run()

    def _DefineUpdate(self):
        pass

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        pass

    def _DefineStatus(self):
        pass

    def _CreateServerConfig(self):
        self.config['SERVER']['transport_config']['allow_insecure'] = self.config['SECURE']
        self.config['SERVER']['port'] = self.config['PORT']
        self.config['SERVER']['name'] = self.config['NAME']
        self.config['SERVER']['access_points'] = self.server_hosts.list()
        with open(self.config['CONF']['SERVER'], 'w') as fp:
            yaml.dump(self.config['SERVER'], fp)

    def _CreateAgentConfig(self):
        self.config['AGENT']['transport_config']['allow_insecure'] = self.config['SECURE']
        self.config['AGENT']['port'] = self.config['PORT']
        self.config['AGENT']['name'] = self.config['NAME']
        self.config['AGENT']['access_points'] = self.agent_hosts.list()
        with open(self.config['CONF']['AGENT'], 'w') as fp:
            yaml.dump(self.config['AGENT'], fp)

    def _CreateControlConfig(self):
        self.config['CONTROL']['transport_config']['allow_insecure'] = self.config['SECURE']
        self.config['CONTROL']['port'] = self.config['PORT']
        self.config['CONTROL']['name'] = self.config['NAME']
        self.config['CONTROL']['hostlist'] = self.control_hosts.list()
        with open(self.config['CONF']['CONTROL'], 'w') as fp:
            yaml.dump(self.config['CONTROL'], fp)
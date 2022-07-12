from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.basic.copy_node import CopyNode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.launchers.launcher import Launcher
from jarvis_cd.basic.mkdir_node import MkdirNode
from jarvis_cd.basic.rm_node import RmNode
from jarvis_cd.basic.link_node import LinkNode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.introspect.detect_networks import DetectNetworks
from jarvis_cd.basic.sleep_node import SleepNode
from jarvis_cd.basic.kill_node import KillNode
from jarvis_cd.basic.echo_node import EchoNode
import yaml

class Daos(Launcher):
    def __init__(self, config_path=None, args=None):
        super().__init__('daos', config_path, args)

    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['hosts'])
        self.agent_hosts = self.all_hosts.SelectHosts(self.config['AGENT']['hosts'])
        self.control_hosts = self.all_hosts.SelectHosts(self.config['CONTROL']['hosts'])

    def _DefineInit(self):
        #Create SCAFFOLD on all nodes
        MkdirNode(self.scaffold_dir, hosts=self.scaffold_hosts, ssh_info=self.ssh_info).Run()
        #Create DAOS_ROOT sybmolic link
        LinkSpackage(self.config['DAOS_SPACK'], self.config['DAOS_ROOT'], hosts=self.scaffold_hosts, ssh_info=self.ssh_info).Run()
        #Generate security certificates
        if self.config['SECURE']:
            gen_certificates_cmd = f"{self.config['DAOS_ROOT']}/lib64/daos/certgen/gen_certificates.sh {self.scaffold_dir}"
            ExecNode(gen_certificates_cmd).Run()
        #View network ifaces
        EchoNode("Detect Network Interfaces").Run()
        DetectNetworks().Run()
        iface = input('Select an initial network interface: ')
        self.config['SERVER']['engines'][0]['fabric_iface'] = iface
        #Generate config files
        self._CreateServerConfig()
        self._CreateAgentConfig()
        self._CreateControlConfig()
        #Copy the certificates+config to all servers
        to_copy = [
            self.all_hosts.Path(),
            self.config['CONF']['AGENT'],
            self.config['CONF']['SERVER'],
            self.config['CONF']['CONTROL'],
            f"{self.scaffold_dir}/jarvis_conf.yaml",
            f"{self.scaffold_dir}/daosCA"
        ]
        CopyNode(to_copy, f"{self.config['SCAFFOLD']}", hosts=self.scaffold_hosts, ssh_info=self.ssh_info).Run()
        #Start dummy DAOS server (on all server nodes)
        EchoNode("Starting DAOS server").Run()
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        EchoNode(server_start_cmd).Run()
        ExecNode(server_start_cmd, hosts=self.server_hosts, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
        SleepNode(6).Run()
        #Format storage
        EchoNode("Formatting DAOS storage").Run()
        storage_format_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg storage format --force -o {self.config['CONF']['CONTROL']}"
        EchoNode(storage_format_cmd)
        ExecNode(storage_format_cmd, hosts=self.server_hosts, sudo=True, ssh_info=self.ssh_info).Run()
        #Get networking options
        EchoNode("Scanning networks").Run()
        network_check_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} network scan -p all"
        EchoNode(network_check_cmd).Run()
        ExecNode(network_check_cmd, sudo=True, shell=True).Run()
        #Link SCAFFOLD to /var/run/daos_agent
        LinkNode(self.scaffold_dir, '/var/run/daos_agent', hosts=self.agent_hosts, sudo=True, ssh_info=self.ssh_info).Run()
        #Create storage pools
        EchoNode("Create storage pools").Run()
        for pool in self.config['POOLS']:
            create_pool_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} pool create -z {pool['size']} --label {pool['label']}"
            EchoNode(create_pool_cmd).Run()
            ExecNode(create_pool_cmd).Run()
        #Start client
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        EchoNode(agent_start_cmd).Run()
        ExecNode(agent_start_cmd, hosts=self.agent_hosts, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
        #Create containers
        EchoNode("Create containers").Run()
        for container in self.config['CONTAINERS']:
            create_container_cmd = [
                f"{self.config['DAOS_ROOT']}/bin/daos container create",
                f"--type {container['type']}",
                f"--pool {container['pool']}",
                f"--label {container['label']}"
            ]
            MkdirNode(container['mount'], hosts=self.agent_hosts, ssh_info=self.ssh_info).Run()
            create_container_cmd = " ".join(create_container_cmd)
            EchoNode(create_container_cmd).Run()
            ExecNode(create_container_cmd).Run()
        self.Stop()

    def _DefineStart(self):
        #Start DAOS server
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        EchoNode(server_start_cmd).Run()
        #ExecNode(server_start_cmd, hosts=self.server_hosts, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
        #Start client
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        EchoNode(agent_start_cmd).Run()
        #ExecNode(agent_start_cmd, hosts=self.agent_hosts, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
        #Mount containers on clients
        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                mkdir_cmd = f"mkdir -p {container['mount']}"
                mount_cmd = [
                    f"{self.config['DAOS_ROOT']}/bin/dfuse",
                    f"--pool {container['pool']}",
                    f"--container {container['label']}",
                    f"-m {container['mount']}"
                ]
                mount_cmd = " ".join(mount_cmd)
                cmds = [mkdir_cmd, mount_cmd]
                EchoNode(mount_cmd).Run()
                #ExecNode(mount_cmd, hosts=self.agent_hosts).Run()

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        #Unmount containers
        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                umount_cmd = f"fusermount3 -u {container['mount']}"
                ExecNode(umount_cmd, hosts=self.agent_hosts, ssh_info=self.ssh_info).Run()
        #Politefully stop servers
        server_stop_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg system stop -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        ExecNode(server_stop_cmd, sudo=True).Run()
        #Kill anything else DAOS spawns
        KillNode('.*daos.*', hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def _DefineStatus(self):
        pass

    def _CreateServerConfig(self):
        server_config = self.config.copy()['SERVER']
        del server_config['hosts']
        server_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        server_config['port'] = self.config['PORT']
        server_config['name'] = self.config['NAME']
        server_config['access_points'] = self.server_hosts.list()
        with open(self.config['CONF']['SERVER'], 'w') as fp:
            yaml.dump(server_config, fp)

    def _CreateAgentConfig(self):
        agent_config = self.config.copy()['AGENT']
        del agent_config['hosts']
        agent_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        agent_config['port'] = self.config['PORT']
        agent_config['name'] = self.config['NAME']
        agent_config['access_points'] = self.agent_hosts.list()
        with open(self.config['CONF']['AGENT'], 'w') as fp:
            yaml.dump(self.config['AGENT'], fp)

    def _CreateControlConfig(self):
        control_config = self.config.copy()['CONTROL']
        del control_config['hosts']
        control_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        control_config['port'] = self.config['PORT']
        control_config['name'] = self.config['NAME']
        control_config['hostlist'] = self.control_hosts.list()
        with open(self.config['CONF']['CONTROL'], 'w') as fp:
            yaml.dump(control_config, fp)
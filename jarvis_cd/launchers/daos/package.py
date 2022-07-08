from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.comm.scp_node import SCPNode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.launchers.launcher import Launcher
from jarvis_cd.basic.link_node import LinkNode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.hardware.host_aliases import FindHostAliases
from jarvis_cd.hardware.detect_networks import DetectNetworks
from jarvis_cd.basic.sleep_node import SleepNode
from jarvis_cd.basic.kill_node import KillNode
import yaml

class Daos(Launcher):
    def __init__(self, config_path=None, args=None):
        super().__init__('daos', config_path, args)

    def _ProcessConfig(self):
        self.all_hosts = Hostfile().LoadHostfile(self.config['HOSTS'])
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['hosts'])
        self.agent_hosts = self.all_hosts.SelectHosts(self.config['AGENT']['hosts'])
        self.control_hosts = self.all_hosts.SelectHosts(self.config['CONTROL']['hosts'])
        self.ssh_info = self.config['SSH']
        self.ssh_info['host_aliases'] = FindHostAliases('Get Aliases', self.all_hosts).Run().GetAliases()
        return

    def _DefineInit(self):
        #Create SCAFFOLD on all nodes
        SSHNode("Make Scaffold Directory", self.server_hosts, f"mkdir -p {self.scaffold_dir}", ssh_info=self.ssh_info).Run()
        #Create DAOS_ROOT sybmolic link
        LinkSpackage("Link Spackage", self.all_hosts, self.config['DAOS_SPACK'], self.config['DAOS_ROOT'], ssh_info=self.ssh_info).Run()
        #Generate security certificates
        gen_certificates_cmd = f"{self.config['DAOS_ROOT']}/lib64/daos/certgen/gen_certificates.sh {self.scaffold_dir}"
        ExecNode('Generate Certificates', gen_certificates_cmd).Run()
        #View network ifaces
        print("Detect Network Interfaces")
        DetectNetworks('Detect Networks').Run()
        iface = input('Select an initial network interface: ')
        self.config['SERVER']['engines'][0]['fabric_iface'] = iface
        #Generate config files
        self._CreateServerConfig()
        self._CreateAgentConfig()
        self._CreateControlConfig()
        #Copy the certificates+config to all servers
        to_copy = [
            self.config['HOSTS'],
            self.config['CONF']['AGENT'],
            self.config['CONF']['SERVER'],
            self.config['CONF']['CONTROL'],
            f"{self.scaffold_dir}/jarvis_conf.yaml",
            f"{self.scaffold_dir}/daosCA"
        ]
        SCPNode('Distribute Configs & Keys', self.all_hosts, to_copy, f"{self.config['SCAFFOLD']}", ssh_info=self.ssh_info).Run()
        #Start dummy DAOS server (on all server nodes)
        print("Starting DAOS server")
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        print(server_start_cmd)
        SSHNode('Start DAOS', self.server_hosts, server_start_cmd, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
        SleepNode('Wait for Server', 6).Run()
        #Format storage
        print("Formatting DAOS storage")
        storage_format_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg storage format --force -o {self.config['CONF']['CONTROL']}"
        print(storage_format_cmd)
        SSHNode('Format DAOS', self.server_hosts, storage_format_cmd, sudo=True, ssh_info=self.ssh_info).Run()
        #Get networking options
        print("Scanning networks")
        network_check_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} network scan -p all"
        print(network_check_cmd)
        ExecNode('Get Networks', network_check_cmd, sudo=True, shell=True).Run()
        #Link SCAFFOLD to /var/run/daos_agent
        link_cmd = f"ln -s {self.scaffold_dir} /var/run/daos_agent"
        SSHNode('Link agent folder', self.agent_hosts, link_cmd, sudo=True, ssh_info=self.ssh_info).Run()
        #Create storage pools
        print("Create storage pools")
        for pool in self.config['POOLS']:
            create_pool_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} pool create -z {pool['size']} --label {pool['label']}"
            print(create_pool_cmd)
            ExecNode('Create Pool', create_pool_cmd).Run()
        #Start client
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        print(agent_start_cmd)
        SSHNode('Start DAOS Agent', self.agent_hosts, agent_start_cmd, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
        #Create containers
        print("Create containers")
        for container in self.config['CONTAINERS']:
            create_container_cmd = [
                f"{self.config['DAOS_ROOT']}/bin/daos container create",
                f"--type {container['type']}",
                f"--pool {container['pool']}",
                f"--label {container['label']}"
            ]
            SSHNode('Create Mount Directory', self.agent_hosts, f"mkdir -p {container['mount']}", ssh_info=self.ssh_info).Run()
            create_container_cmd = " ".join(create_container_cmd)
            print(create_container_cmd)
            ExecNode('Create Container', create_container_cmd).Run()
        self.Stop()

    def _DefineStart(self):
        #Start DAOS server
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        print(server_start_cmd)
        #SSHNode('Start DAOS', self.server_hosts, server_start_cmd, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
        #Start client
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        print(agent_start_cmd)
        #SSHNode('Start DAOS Agent', self.agent_hosts, agent_start_cmd, sudo=True, exec_async=True, ssh_info=self.ssh_info).Run()
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
                print(mount_cmd)
                #SSHNode('Mount Container', self.agent_hosts, mount_cmd).Run()

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        #Unmount containers
        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                umount_cmd = f"fusermount3 -u {container['mount']}"
                SSHNode('Unmount Container', self.agent_hosts, umount_cmd, ssh_info=self.ssh_info).Run()
        #Politefully stop servers
        server_stop_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg system stop -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        ExecNode('Stop DAOS', server_stop_cmd, sudo=True).Run()
        #Kill anything else DAOS spawns
        kill_cmd = '$JARVIS_ROOT/bin/jarvis-kill ".*daos.*"'
        SSHNode('Kill DAOS', self.all_hosts, kill_cmd, sudo=True, ssh_info=self.ssh_info).Run()

    def _DefineStatus(self):
        pass

    def _CreateServerConfig(self):
        server_config = self.config.copy()['SERVER']
        del server_config['hosts']
        server_config['transport_config']['allow_insecure'] = self.config['SECURE']
        server_config['port'] = self.config['PORT']
        server_config['name'] = self.config['NAME']
        server_config['access_points'] = self.server_hosts.list()
        with open(self.config['CONF']['SERVER'], 'w') as fp:
            yaml.dump(server_config, fp)

    def _CreateAgentConfig(self):
        agent_config = self.config.copy()['AGENT']
        del agent_config['hosts']
        agent_config['transport_config']['allow_insecure'] = self.config['SECURE']
        agent_config['port'] = self.config['PORT']
        agent_config['name'] = self.config['NAME']
        agent_config['access_points'] = self.agent_hosts.list()
        with open(self.config['CONF']['AGENT'], 'w') as fp:
            yaml.dump(self.config['AGENT'], fp)

    def _CreateControlConfig(self):
        control_config = self.config.copy()['CONTROL']
        del control_config['hosts']
        control_config['transport_config']['allow_insecure'] = self.config['SECURE']
        control_config['port'] = self.config['PORT']
        control_config['name'] = self.config['NAME']
        control_config['hostlist'] = self.control_hosts.list()
        with open(self.config['CONF']['CONTROL'], 'w') as fp:
            yaml.dump(control_config, fp)
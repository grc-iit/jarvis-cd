from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.launcher.application import Application
from jarvis_cd.fs.mkdir_node import MkdirNode
from jarvis_cd.fs.rm_node import RmNode
from jarvis_cd.fs.link_node import LinkNode
from jarvis_cd.spack.link_package import LinkSpackage
from jarvis_cd.introspect.detect_networks import DetectNetworks
from jarvis_cd.basic.sleep_node import SleepNode
from jarvis_cd.shell.kill_node import KillNode
from jarvis_cd.basic.echo_node import EchoNode
from jarvis_cd.fs.fs import UnmountFS
from jarvis_cd.serialize.yaml_file import YAMLFile
from jarvis_cd.installer.git_node import GitNode,GitOps
from jarvis_cd.installer.patch_node import PatchNode
from jarvis_cd.hostfile import Hostfile
import os

class Daos(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['hosts'])
        self.agent_hosts = self.all_hosts.SelectHosts(self.config['AGENT']['hosts'])
        self.control_hosts = self.all_hosts.SelectHosts(self.config['CONTROL']['hosts'])
        self.daos_hosts = self.server_hosts
        if 'DAOS_HOSTS' in self.config:
            self.daos_hosts = Hostfile().LoadHostfile(self.config['DAOS_HOSTS'])
        self.pools_by_label = {}

    def Install(self, sys):
        url = 'https://github.com/daos-stack/daos.git'
        branch = 'release/2.0'
        GitNode(url, "/tmp/daos", GitOps.CLONE, branch=branch, hosts=self.all_hosts).Run()
        if sys == 'ubuntu20':
            ExecNode(f"bash /tmp/daos/utils/scripts/install-ubuntu20.sh", hosts=self.all_hosts, shell=True).Run()
        elif sys == 'centos8':
            PatchNode(os.path.join(self.package_root, "patches", "centos8_deps.patch"), "/tmp/daos", hosts=self.all_hosts).Run()
            ExecNode(f"bash /tmp/daos/utils/scripts/install-el8.sh", hosts=self.all_hosts, shell=True).Run()
        elif sys == 'centos7':
            ExecNode(f"bash /tmp/daos/utils/scripts/install-centos7.sh", hosts=self.all_hosts, shell=True).Run()
        elif sys == 'leap15':
            ExecNode(f"bash /tmp/daos/utils/scripts/install-leap15.sh", hosts=self.all_hosts, shell=True).Run()
        else:
            print("Requires variant OS")
            exit()
        ExecNode(f"spack install daos", hosts=self.jarvis_hosts).Run()

    def _InstallArgs(self, parser):
        parser.add_argument('--sys', metavar='os', default='centos8', help='OS for installing dependencies (centos8,centos7,ubuntu20,leap15)')

    def _DefineInit(self):
        #Create SCAFFOLD on all nodes
        MkdirNode(self.scaffold_dir, hosts=self.scaffold_hosts).Run()
        #Create DAOS_ROOT sybmolic link
        LinkSpackage(self.config['DAOS_SPACK'], self.config['DAOS_ROOT'], hosts=self.scaffold_hosts).Run()
        #Generate security certificates
        if self.config['SECURE']:
            self._CreateCertificates()
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
            self.daos_hosts.Path(),
            self.all_hosts.Path(),
            self.config['CONF']['AGENT'],
            self.config['CONF']['SERVER'],
            self.config['CONF']['CONTROL'],
            f"{self.scaffold_dir}/jarvis_conf.yaml",
            f"{self.scaffold_dir}/daosCA" if self.config['SECURE'] else None
        ]
        CopyNode(to_copy, f"{self.config['SCAFFOLD']}", hosts=self.scaffold_hosts).Run()
        #Start dummy DAOS server (on all server nodes)
        self._StartServers()
        #Format storage
        EchoNode("Formatting DAOS storage").Run()
        self._FormatStorage()
        #Get networking options
        EchoNode("Scanning networks").Run()
        self._ScanNetworks()
        #Link SCAFFOLD to /var/run/daos_agent
        LinkNode(self.scaffold_dir, '/var/run/daos_agent', hosts=self.agent_hosts, sudo=True).Run()
        #Create storage pools
        EchoNode("Create storage pools").Run()
        for pool in self.config['POOLS']:
            self._CreatePool(pool)
        #Start client
        self._StartAgents()
        #Create containers
        EchoNode("Create containers").Run()
        for container in self.config['CONTAINERS']:
            self._CreateContainer(container)
        #Stop daos servers
        self.Stop()

    def _DefineStart(self):
        #Start DAOS server
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        EchoNode(server_start_cmd).Run()
        ExecNode(server_start_cmd, hosts=self.server_hosts, sudo=True, exec_async=True).Run()
        SleepNode(3).Run()
        #Start client
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        EchoNode(agent_start_cmd).Run()
        ExecNode(agent_start_cmd, hosts=self.agent_hosts, sudo=True, exec_async=True).Run()
        SleepNode(3).Run()
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
                EchoNode(mount_cmd).Run()
                ExecNode(mount_cmd, hosts=self.agent_hosts).Run()

    def _DefineClean(self):
        to_rm = [
            self.config['CONF']['AGENT'],
            self.config['CONF']['SERVER'],
            self.config['CONF']['CONTROL'],
            f"{self.scaffold_dir}/daosCA",
            os.path.join(self.scaffold_dir, '*.sock'),
            os.path.join(self.scaffold_dir, '*.log')
        ]
        RmNode(to_rm, hosts=self.all_hosts).Run()

        for engine in self.config['SERVER']['engines']:
            for storage in engine['storage']:
                for key,mount in storage.items():
                    if 'mount' in key:
                        RmNode(mount, hosts=self.server_hosts, sudo=True).Run()

        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                RmNode(container['mount'], hosts=self.agent_hosts, sudo=True).Run()

    def _DefineStop(self):
        #Politefully stop servers
        server_stop_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg system stop -o {self.config['CONF']['CONTROL']} -d {self.config['SCAFFOLD']}"
        ExecNode(server_stop_cmd, sudo=True).Run()
        #Unmount containers
        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                umount_cmd = f"fusermount3 -u {container['mount']}"
                ExecNode(umount_cmd, hosts=self.agent_hosts).Run()
        #Kill anything else DAOS spawns
        KillNode('.*daos.*', hosts=self.all_hosts).Run()
        #Unmount SCM
        for engine in self.config['SERVER']['engines']:
            for storage in engine['storage']:
                for key, mount in storage.items():
                    if 'mount' in key:
                        UnmountFS(mount, hosts=self.server_hosts).Run()

    def _DefineStatus(self):
        cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o ${SCAFFOLD}/daos_control.yaml system query -v"
        ExecNode(cmd, hosts=self.all_hosts).Run()

    def _StartServers(self):
        EchoNode("Starting DAOS server").Run()
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']} -d {self.config['SCAFFOLD']}"
        EchoNode(server_start_cmd).Run()
        ExecNode(server_start_cmd, hosts=self.server_hosts, sudo=True, exec_async=True).Run()
        SleepNode(3).Run()

    def _StartAgents(self):
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        EchoNode(agent_start_cmd).Run()
        ExecNode(agent_start_cmd, hosts=self.agent_hosts, sudo=True, exec_async=True).Run()

    def _CreateCertificates(self):
        gen_certificates_cmd = f"{self.config['DAOS_ROOT']}/lib64/daos/certgen/gen_certificates.sh {self.scaffold_dir}"
        ExecNode(gen_certificates_cmd).Run()

    def _FormatStorage(self):
        storage_format_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg storage format --force -o {self.config['CONF']['CONTROL']}"
        EchoNode(storage_format_cmd)
        ExecNode(storage_format_cmd, sudo=True).Run()

    def _ScanNetworks(self):
        network_check_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} network scan -p all"
        EchoNode(network_check_cmd).Run()
        ExecNode(network_check_cmd, sudo=True, shell=True).Run()

    def _CreatePool(self, pool_info):
        create_pool_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} pool create -z {pool_info['size']} --label {pool_info['label']}"
        EchoNode(create_pool_cmd).Run()
        ExecNode(create_pool_cmd).Run()

    def _CreateContainer(self, container_info):
        create_container_cmd = [
            f"{self.config['DAOS_ROOT']}/bin/daos container create",
            f"--type {container_info['type']}",
            f"--pool {container_info['pool']}",
            f"--label {container_info['label']}"
        ]
        MkdirNode(container_info['mount'], hosts=self.agent_hosts).Run()
        create_container_cmd = " ".join(create_container_cmd)
        EchoNode(create_container_cmd).Run()
        ExecNode(create_container_cmd).Run()

    def _CreateServerConfig(self):
        server_config = self.config.copy()['SERVER']
        del server_config['hosts']
        server_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        server_config['port'] = self.config['PORT']
        server_config['name'] = self.config['NAME']
        server_config['access_points'] = self.daos_hosts.list()
        YAMLFile(self.config['CONF']['SERVER']).Save(server_config)

    def _CreateAgentConfig(self):
        agent_config = self.config.copy()['AGENT']
        del agent_config['hosts']
        agent_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        agent_config['port'] = self.config['PORT']
        agent_config['name'] = self.config['NAME']
        agent_config['access_points'] = self.agent_hosts.list()
        YAMLFile(self.config['CONF']['AGENT']).Save(agent_config)

    def _CreateControlConfig(self):
        control_config = self.config.copy()['CONTROL']
        del control_config['hosts']
        control_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        control_config['port'] = self.config['PORT']
        control_config['name'] = self.config['NAME']
        control_config['hostlist'] = self.control_hosts.list()
        YAMLFile(self.config['CONF']['CONTROL']).Save(control_config)

    def GetPoolUUID(self, pool_label):
        cmd = f"{self.config['DAOS_ROOT']}/bin/dmg pool list --verbose -o {self.config['CONF']['CONTROL']}"
        node = ExecNode(cmd).Run()
        for line in node.GetLocalStdout():
            words = line.split()
            cur_pool_label = words[0]
            if cur_pool_label == pool_label:
                pool_uuid = words[1]
                return pool_uuid
        return None

    def GetContainerUUID(self, pool_uuid, container_label):
        cmd = f"{self.config['DAOS_ROOT']}/bin/daos cont list {pool_uuid}"
        node = ExecNode(cmd).Run()
        for line in node.GetLocalStdout():
            words = line.split()
            cur_container_label = words[1]
            if cur_container_label == container_label:
                container_uuid = words[0]
                return container_uuid
        return None
from jarvis_cd import *
import os

class Daos(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['hosts'])
        self.agent_hosts = self.all_hosts.SelectHosts(self.config['AGENT']['hosts'])
        self.control_hosts = self.all_hosts.SelectHosts(self.config['CONTROL']['hosts'])
        if 'socket_dir' in self.config['SERVER'] and 'runtime_dir' in self.config['AGENT']:
            self.server_sockets = self.config['SERVER']['socket_dir']
            self.agent_sockets = self.config['AGENT']['runtime_dir']
        else:
            self.server_sockets = '/var/run/daos_server'
            self.agent_sockets = '/var/run/daos_agent'
        self.pools_by_label = {}

    def _DefineInit(self):
        #Create DAOS_ROOT sybmolic link
        if 'DAOS_SPACK' in self.config:
            LinkSpackage(self.config['DAOS_SPACK'], self.config['DAOS_ROOT'], hosts=self.all_hosts).Run()
        elif 'DAOS_SCSPKG' in self.config:
            LinkScspkg(self.config['DAOS_SCSPKG'], self.config['DAOS_ROOT'], hosts=self.all_hosts).Run()
        else:
            self.config['DAOS_ROOT'] = '/usr'
        #Create socket directories
        MkdirNode(self.server_sockets, hosts=self.all_hosts, sudo=True).Run()
        MkdirNode(self.agent_sockets, hosts=self.all_hosts, sudo=True).Run()
        #Generate security certificates
        if self.config['SECURE']:
            self._CreateCertificates()
        #View network ifaces
        EchoNode("Detect Network Interfaces").Run()
        if 'fabric_iface' not in self.config['SERVER']['engines'][0] or self.config['SERVER']['engines'][0]['fabric_iface'] is None:
            if 'fabric_ifaces' not in self.config['SERVER']['engines'][0] or self.config['SERVER']['engines'][0]['fabric_ifaces'] is None:
                DetectNetworks().Run()
                iface = input('Initial Network Interface: ')
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
            f"{self.shared_dir}/jarvis_conf.yaml",
            f"{self.shared_dir}/daosCA" if self.config['SECURE'] else None
        ]
        CopyNode(to_copy, self.shared_dir, hosts=self.shared_hosts).Run()
        #Prepare storage
        if 'PREPARE_STORAGE' in self.config:
            PrepareStorage(self.config['PREPARE_STORAGE'], hosts=self.server_hosts).Run()
        #Start dummy DAOS server (on all server nodes)
        self._StartServers()
        # Get networking options
        EchoNode("Scanning networks").Run()
        self._ScanNetworks()
        iface = input('Get actual network interface: ')
        #Format storage
        EchoNode("Formatting DAOS storage").Run()
        self._FormatStorage()
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
        self._StartServers()
        #Start client
        self._StartAgents()
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
                ExecNode(mount_cmd, hosts=self.agent_hosts).Run()

    def _DefineClean(self):
        to_rm = [
            self.config['CONF']['AGENT'],
            self.config['CONF']['SERVER'],
            self.config['CONF']['CONTROL'],
            f"{self.shared_dir}/daosCA",
            self.config['DAOS_ROOT']
        ]
        RmNode(to_rm, hosts=self.all_hosts).Run()
        to_rm = [
            os.path.join(self.server_sockets, '*.sock'),
            os.path.join(self.agent_sockets, '*.log')
        ]
        RmNode(to_rm, hosts=self.all_hosts, sudo=True).Run()
        if 'PREPARE_STORAGE' in self.config:
            UnprepareStorage(self.config['PREPARE_STORAGE'], hosts=self.server_hosts).Run()
        for engine in self.config['SERVER']['engines']:
            for storage in engine['storage']:
                for key,mount in storage.items():
                    if 'mount' in key:
                        UnmountFS(mount, hosts=self.server_hosts).Run()
                        RmNode(mount, hosts=self.server_hosts, sudo=True).Run()

        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                RmNode(container['mount'], hosts=self.agent_hosts, sudo=True).Run()

    def _DefineStop(self):
        #Politefully stop servers
        server_stop_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg system stop -o {self.config['CONF']['CONTROL']}"
        ExecNode(server_stop_cmd, sudo=True).Run()
        #Unmount containers
        for container in self.config['CONTAINERS']:
            if 'mount' in container and container['mount'] is not None:
                umount_cmd = f"fusermount3 -u {container['mount']}"
                ExecNode(umount_cmd, hosts=self.agent_hosts).Run()
        #Kill anything else DAOS spawns
        KillNode('.*daos.*', hosts=self.all_hosts).Run()

    def _DefineStatus(self):
        cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.shared_dir}/daos_control.yaml system query -v"
        ExecNode(cmd, hosts=self.all_hosts).Run()

    def _StartServers(self):
        EchoNode("Starting DAOS server").Run()
        server_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_server start -o {self.config['CONF']['SERVER']}"
        ExecNode(server_start_cmd, hosts=self.server_hosts, sudo=True, exec_async=True).Run()
        EchoNode(self.server_hosts).Run()
        SleepNode(3).Run()

    def _StartAgents(self):
        agent_start_cmd = f"{self.config['DAOS_ROOT']}/bin/daos_agent start -o {self.config['CONF']['AGENT']}"
        ExecNode(agent_start_cmd, hosts=self.agent_hosts, sudo=True, exec_async=True).Run()
        SleepNode(3).Run()

    def _CreateCertificates(self):
        gen_certificates_cmd = f"{self.config['DAOS_ROOT']}/lib64/daos/certgen/gen_certificates.sh {self.shared_dir}"
        ExecNode(gen_certificates_cmd).Run()

    def _FormatStorage(self):
        storage_format_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg storage format --force -o {self.config['CONF']['CONTROL']}"
        ExecNode(storage_format_cmd, sudo=True).Run()

    def _ScanNetworks(self):
        network_check_cmd = f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} network scan -p all"
        ExecNode(network_check_cmd, sudo=True, shell=True).Run()

    def _CreatePool(self, pool_info):
        create_pool_cmd = [
            f"{self.config['DAOS_ROOT']}/bin/dmg -o {self.config['CONF']['CONTROL']} pool create",
            f"-z {pool_info['size']}",
            f"--label {pool_info['label']}"#,
            #f"--nranks={len(self.server_hosts)}"
        ]
        create_pool_cmd = " ".join(create_pool_cmd)
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
        ExecNode(create_container_cmd).Run()

    def _CreateServerConfig(self):
        server_config = self.config.copy()['SERVER']
        del server_config['hosts']
        server_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        server_config['port'] = self.config['PORT']
        server_config['name'] = self.config['NAME']
        server_config['access_points'] = self.server_hosts.ip_list()
        YAMLFile(self.config['CONF']['SERVER']).Save(server_config)

    def _CreateAgentConfig(self):
        agent_config = self.config.copy()['AGENT']
        del agent_config['hosts']
        agent_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        agent_config['port'] = self.config['PORT']
        agent_config['name'] = self.config['NAME']
        agent_config['access_points'] = self.agent_hosts.ip_list()
        YAMLFile(self.config['CONF']['AGENT']).Save(agent_config)

    def _CreateControlConfig(self):
        control_config = self.config.copy()['CONTROL']
        del control_config['hosts']
        control_config['transport_config']['allow_insecure'] = not self.config['SECURE']
        control_config['port'] = self.config['PORT']
        control_config['name'] = self.config['NAME']
        control_config['hostlist'] = self.control_hosts.ip_list()
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
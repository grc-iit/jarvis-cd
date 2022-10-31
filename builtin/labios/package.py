from jarvis_cd import *
from jarvis_repos.builtin.memcached.package import Memcached
from jarvis_repos.builtin.nats_server.package import NatsServer
import os

class Labios(Application):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.nats_client_hosts = self.all_hosts.SelectHosts(self.config['NATS_CLIENT']['HOSTS'])
        self.nats_server_hosts = self.all_hosts.SelectHosts(self.config['NATS_SERVER']['HOSTS'])
        self.memcached_client_hosts = self.all_hosts.SelectHosts(self.config['MEMCACHED_CLIENT']['HOSTS'])
        self.memcached_server_hosts = self.all_hosts.SelectHosts(self.config['MEMCACHED_SERVER']['HOSTS'])

        self.worker_hosts = self.all_hosts.SelectHosts(self.config['WORKER']['HOSTS'])
        self.worker_manager_hosts = self.all_hosts.SelectHosts(self.config['WORKER_MANAGER']['HOSTS'])
        self.task_scheduler_hosts = self.all_hosts.SelectHosts(self.config['TASK_SCHEDULER']['HOSTS'])
        self.server_hosts = self.all_hosts.SelectHosts(self.config['SERVER']['HOSTS'])
        self.client_hosts = self.all_hosts.SelectHosts(self.config['CLIENT']['HOSTS'])

        self.labios_build = self.config['LABIOS']['BUILD']

    def _Scaffold(self):
        super()._Scaffold()

    def _DefineInit(self):
        #Create SCAFFOLD on all nodes
        MkdirNode(self.shared_dir, hosts=self.all_hosts).Run()

        #Mkdir all scaffold directories
        MkdirNode(self.config['MEMCACHED_CLIENT']['SCAFFOLD']).Run()
        MkdirNode(self.config['MEMCACHED_SERVER']['SCAFFOLD']).Run()
        MkdirNode(self.config['NATS_CLIENT']['SCAFFOLD']).Run()
        MkdirNode(self.config['NATS_SERVER']['SCAFFOLD']).Run()

        #Create jarvis_conf for memcached and nats
        self._GenMemcachedConfig(self.config['MEMCACHED_CLIENT'])
        self._GenMemcachedConfig(self.config['MEMCACHED_SERVER'])
        self._GenNatsConfig(self.config['NATS_CLIENT'])
        self._GenNatsConfig(self.config['NATS_SERVER'])

        #Initialize memcached and NATS
        Memcached(self.config['MEMCACHED_CLIENT']['SCAFFOLD']).Init()
        Memcached(self.config['MEMCACHED_SERVER']['SCAFFOLD']).Init()
        NatsServer(self.config['NATS_CLIENT']['SCAFFOLD']).Init()
        NatsServer(self.config['NATS_SERVER']['SCAFFOLD']).Init()

        #Create LABIOS configuration
        self._GenLabiosConfig()

    def _DefineStart(self):
        Memcached(self.config['MEMCACHED_CLIENT']['SCAFFOLD']).Start()
        Memcached(self.config['MEMCACHED_SERVER']['SCAFFOLD']).Start()
        NatsServer(self.config['NATS_CLIENT']['SCAFFOLD']).Start()
        NatsServer(self.config['NATS_SERVER']['SCAFFOLD']).Start()
        MPINode(f"{self.labios_build}/labios_worker {self.config['LABIOS']['CONF']}",
                self.config['WORKER']['NPROCS'],
                hosts=self.worker_hosts, exec_async=True).Run()
        MPINode(f"{self.labios_build}/labios_worker_manager {self.config['LABIOS']['CONF']}",
                self.config['WORKER_MANAGER']['NPROCS'],
                hosts=self.worker_manager_hosts, exec_async=True).Run()
        MPINode(f"{self.labios_build}/labios_task_scheduler {self.config['LABIOS']['CONF']}",
                self.config['TASK_SCHEDULER']['NPROCS'],
                hosts=self.task_scheduler_hosts, exec_async=True).Run()
        MPINode(f"{self.labios_build}/labios_server {self.config['LABIOS']['CONF']}",
                self.config['SERVER']['NPROCS'],
                hosts=self.server_hosts, exec_async=True).Run()
        MPINode(f"{self.labios_build}/labios_client {self.config['LABIOS']['CONF']}",
                self.config['CLIENT']['NPROCS'],
                hosts=self.client_hosts, exec_async=True).Run()

    def _DefineClean(self):
        pass

    def _DefineStop(self):
        Memcached(self.config['MEMCACHED_CLIENT']['SCAFFOLD']).Stop()
        Memcached(self.config['MEMCACHED_SERVER']['SCAFFOLD']).Stop()
        NatsServer(self.config['NATS_CLIENT']['SCAFFOLD']).Stop()
        NatsServer(self.config['NATS_SERVER']['SCAFFOLD']).Stop()
        KillNode('.*labios_worker.*').Run()
        KillNode('.*labios_worker_manager.*').Run()
        KillNode('.*labios_task_scheduler.*').Run()
        KillNode('.*labios_server.*').Run()
        KillNode('.*labios_client.*').Run()

    def _DefineStatus(self):
        pass

    def _GenMemcachedConfig(self, mdict):
        mdict.update(self.config['MEMCACHED'])
        mdict['MEMCACHED_HOST'] = mdict['HOSTS']
        mdict['HOSTS'] = self.all_hosts.SelectHosts(mdict['HOSTS']).hostname_list()
        Memcached(mdict['SCAFFOLD']).Scaffold(config=mdict)

    def _GenNatsConfig(self, ndict):
        ndict.update(self.config['NATS'])
        ndict['NATS_HOST'] = ndict['HOSTS']
        ndict['HOSTS'] = self.all_hosts.SelectHosts(ndict['HOSTS']).hostname_list()
        NatsServer(ndict['SCAFFOLD']).Scaffold(config=ndict)

    def _GenLabiosConfig(self):
        ldict = self.config['LABIOS'].copy()
        del ldict['CONF']
        ldict['NATS_URL_CLIENT'] = f"nats://{self.nats_client_hosts.ip_list()[0]}:{self.config['NATS_CLIENT']['PORT']}/"
        ldict['NATS_URL_SERVER'] = f"nats://{self.nats_server_hosts.ip_list()[0]}:{self.config['NATS_SERVER']['PORT']}/"
        ldict['MEMCACHED_URL_CLIENT'] = f"--SERVER={self.memcached_client_hosts.ip_list()[0]}:{self.config['MEMCACHED_CLIENT']['PORT']}"
        ldict['MEMCACHED_URL_SERVER'] = f"--SERVER={self.memcached_server_hosts.ip_list()[0]}:{self.config['MEMCACHED_SERVER']['PORT']}"
        YAMLFile(self.config['LABIOS']['CONF']).Save(ldict)
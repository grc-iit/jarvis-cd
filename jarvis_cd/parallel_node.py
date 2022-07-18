from jarvis_cd.exception import Error, ErrorCode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.node import Node
from jarvis_cd.jarvis_manager import JarvisManager
import sys,os
import getpass

class ParallelNode(Node):
    def __init__(self, hosts=None, affinity=None, sleep_period_ms=100, max_retries=0, cwd=None, shell=False, sudo=False,
                 exec_async=False, **kwargs):
        super().__init__(**kwargs)

        # Make sure hosts in proper format
        if hosts is None:
            hosts = []
        if isinstance(hosts, list):
            self.hosts = hosts
        elif isinstance(hosts, str):
            self.hosts = [hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHExecNode hosts", type(hosts))

        username = None
        pkey = None
        password = None
        port = None

        ssh_info = JarvisManager.GetInstance().GetSSHInfo()
        host_aliases = ['localhost']

        # Prioritize ssh_info structure
        if ssh_info is not None:
            if 'username' in ssh_info:
                username = ssh_info['username']
            if 'key' in ssh_info and 'key_dir' in ssh_info:
                pkey = os.path.join(ssh_info['key_dir'], ssh_info['key'])
            if 'password' in ssh_info:
                password = ssh_info['password']
            if 'port' in ssh_info:
                port = int(ssh_info['port'])
            if 'host_aliases' in ssh_info:
                if isinstance(ssh_info['host_aliases'], list):
                    host_aliases += ssh_info['host_aliases']
                else:
                    host_aliases.append(ssh_info['host_aliases'])

        # Fill in defaults for username, password, and pkey
        self.pkey = pkey
        self.password = password
        self.sudo = sudo
        self.username = username
        self.port = port
        self.shell = shell
        self.host_aliases = host_aliases
        self.affinity = affinity
        self.sleep_period_ms = sleep_period_ms
        self.max_retries = max_retries
        self.exec_async = exec_async
        self.cwd = cwd
        self.ssh_info = ssh_info

        #Do SSH only if the host list contains more than host aliases
        self.do_ssh = any(host not in host_aliases for host in self.hosts)

from jarvis_cd.exception import Error, ErrorCode
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.node import Node
import sys,os
import getpass

class ParallelNode(Node):
    def __init__(self, hosts=None, username=None, pkey=None, password=None, port=22,
                 sudo=False, shell=True, host_aliases=None, ssh_info=None,
                 affinity=None, sleep_period_ms=100, max_retries=0, cwd=None,
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

        # Make sure host_aliases is not None
        if host_aliases is None:
            host_aliases = ['localhost']

        # Prioritize ssh_info structure
        if ssh_info is not None:
            if 'username' in ssh_info:
                username = ssh_info['username']
            if 'key' in ssh_info and 'key_dir' in ssh_info:
                pkey = os.path.join(ssh_info['key_dir'], ssh_info['key'])
            if 'password' in ssh_info:
                password = password
            if 'port' in ssh_info:
                port = ssh_info['port']
            if 'sudo' in ssh_info:
                sudo = ssh_info['sudo']
            if 'shell' in ssh_info:
                shell = ssh_info['shell']
            if 'host_aliases' in ssh_info:
                if isinstance(ssh_info['host_aliases'], list):
                    host_aliases += ssh_info['host_aliases']
                else:
                    host_aliases.append(ssh_info['host_aliases'])

        # Fill in defaults for username, password, and pkey
        if username is None:
            username = getpass.getuser()
        if password is None and pkey is None:
            pkey = f"{os.environ['HOME']}/.ssh/id_rsa"

        self.pkey = pkey
        self.password = password
        self.sudo = sudo
        self.username = username
        self.port = int(port)
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

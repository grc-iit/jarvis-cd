from jarvis_cd.hostfile import Hostfile
from jarvis_cd.yaml_conf import YAMLConfig
from jarvis_cd.hardware.host_aliases import FindHostAliases
from jarvis_cd.exception import Error, ErrorCode
import getpass
import sys,os

class SSHConfig(YAMLConfig):
    def DefaultConfigPath(self, conf_type='default'):
        return os.path.join(self.jarvis_root, 'comm', 'conf', f"{conf_type}.yaml")

    def _ProcessConfig(self):
        self.public_key = None
        self.private_key = None
        self.username = getpass.getuser()
        self.password = None
        self.port = 22
        self.host_aliases = None
        self.all_hosts = None
        self.ssh_info = None

        if 'HOSTS' in self.config and 'SSH' in self.config:
            self.all_hosts = Hostfile().Load(self.config['HOSTS'])
            self.ssh_info = self.config['SSH']
            self.ssh_info['host_aliases'] = FindHostAliases('Get Aliases', self.all_hosts).Run().GetAliases()
            self.host_aliases = self.ssh_info['host_aliases']

        if self.ssh_info is not None and 'key' in self.ssh_info and 'key_dir' in self.ssh_info:
            self.public_key = self._GetPublicKey(self.ssh_info['key_dir'], self.ssh_info['key'])
            self.private_key = self._GetPublicKey(self.ssh_info['key_dir'], self.ssh_info['key'])

    def _GetPublicKey(self, key_dir, key_name):
        return f'{key_dir}/{key_name}.pub'

    def _GetPrivateKey(self, key_dir, key_name):
        return f'{key_dir}/{key_name}'

class SSHArgs:
    def _ProcessArgs(self, hosts=None, username=None, pkey=None, password=None, port=22,
         sudo=False, shell=True, host_aliases=None, ssh_info=None):

        #Make sure hosts in proper format
        if hosts is None:
            hosts = []
        if isinstance(hosts, list):
            self.hosts = hosts
        elif isinstance(hosts, str):
            self.hosts = [hosts]
        elif isinstance(hosts, Hostfile):
            self.hosts = hosts.list()
        else:
            raise Error(ErrorCode.INVALID_TYPE).format("SSHNode hosts", type(hosts))

        # Make sure host_aliases is not None
        if host_aliases is None:
            host_aliases = []

        #Prioritize ssh_info structure
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
        self.do_ssh = len(self.hosts) == 0
        self.shell = shell
        self.host_aliases = host_aliases


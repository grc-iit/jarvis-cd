from jarvis_cd.hostfile import Hostfile
from jarvis_cd.yaml_conf import YAMLConfig
from jarvis_cd.introspect.host_aliases import FindHostAliases
from jarvis_cd.exception import Error, ErrorCode
import getpass
import sys,os

class SSHConfig(YAMLConfig):
    def DefaultConfigPath(self, conf_type='default'):
        return os.path.join(self.jarvis_root, 'comm', 'conf', f"{conf_type}.yaml")

    def _Scaffold(self):
        return

    def _ProcessConfig(self):
        self.public_key = None
        self.private_key = None
        self.username = getpass.getuser()
        self.password = None
        self.port = 22
        self.host_aliases = None
        self.all_hosts = None
        self.ssh_info = None

        if 'HOSTS' in self.config:
            self.all_hosts = Hostfile().Load(self.config['HOSTS'])
        if 'SSH' in self.config:
            self.ssh_info = self.config['SSH']
            self.ssh_info['host_aliases'] = FindHostAliases(self.all_hosts).Run().GetAliases()
            self.host_aliases = self.ssh_info['host_aliases']

        if self.ssh_info is not None and 'key' in self.ssh_info and 'key_dir' in self.ssh_info:
            self.public_key = self._GetPublicKey(self.ssh_info['key_dir'], self.ssh_info['key'])
            self.private_key = self._GetPublicKey(self.ssh_info['key_dir'], self.ssh_info['key'])

    def _GetPublicKey(self, key_dir, key_name):
        return f'{key_dir}/{key_name}.pub'

    def _GetPrivateKey(self, key_dir, key_name):
        return f'{key_dir}/{key_name}'
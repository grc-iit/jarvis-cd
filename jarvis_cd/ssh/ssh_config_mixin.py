from jarvis_cd.hostfile import Hostfile
from jarvis_cd.yaml_conf import YAMLConfig
from jarvis_cd.introspect.host_aliases import FindHostAliases
from jarvis_cd.jarvis_manager import JarvisManager
from jarvis_cd.ssh.openssh.openssh_config import FromOpenSSHConfig
from jarvis_cd.ssh.openssh.util import GetPublicKey,GetPrivateKey
from jarvis_cd.exception import Error, ErrorCode
import getpass
import sys,os

class SSHConfigMixin(YAMLConfig):
    def _Scaffold(self):
        return

    def _ProcessConfig(self):
        self._ProcessSSHConfig()

    def _ProcessSSHConfig(self):
        self.ssh_info = {}

        if 'HOSTS' in self.config:
            self.all_hosts = Hostfile().Load(self.config['HOSTS'])
        self.ssh_info = FromOpenSSHConfig(self.all_hosts).Run().GetConfig()
        if 'SSH' in self.config and 'primary' in self.config['SSH']:
            self.ssh_info.update(self.config['SSH']['primary'])
        if len(self.ssh_info):
            if 'host_aliases' not in self.ssh_info:
                self.ssh_info['host_aliases'] = []
            self.ssh_info['host_aliases'] += FindHostAliases(self.all_hosts).Run().GetAliases()
            self.host_aliases = self.ssh_info['host_aliases']
        JarvisManager.GetInstance().SetSSHInfo(self.ssh_info)

    def _GetPublicKey(self, key_dir, key_name):
        return GetPublicKey(key_dir, key_name)

    def _GetPrivateKey(self, key_dir, key_name):
        return GetPrivateKey(key_dir, key_name)
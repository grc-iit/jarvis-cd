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
    def DefaultConfigPath(self, conf_type='default'):
        return os.path.join(self.jarvis_root, 'mpi', 'conf', f"{conf_type}.yaml")

    def _Scaffold(self):
        return

    def _ProcessConfig(self):
        self._ProcessSSHConfig()

    def _ProcessSSHConfig(self):
        self.all_hosts = None
        self.scaffold_hosts = None
        self.jarvis_hosts = None
        self.ssh_info = FromOpenSSHConfig(self.all_hosts).GetConfig()
        self.jarvis_shared = False
        self.scaffold_shared = False

        if 'HOSTS' in self.config:
            self.all_hosts = Hostfile().Load(self.config['HOSTS'])
            self.scaffold_hosts = self.all_hosts
            self.jarvis_hosts = self.all_hosts
            if 'SCAFFOLD_SHARED' in self.config and self.config['SCAFFOLD_SHARED']:
                self.scaffold_hosts = Hostfile().Load(['localhost'])
            if 'JARVIS_SHARED' in os.environ and bool(int(os.environ['JARVIS_SHARED'])):
                self.jarvis_hosts = Hostfile().Load(['localhost'])
        if 'SSH' in self.config:
            self.ssh_info = self.config['SSH']
            self.ssh_info['host_aliases'] = FindHostAliases(self.all_hosts).Run().GetAliases()
            self.host_aliases = self.ssh_info['host_aliases']
        JarvisManager.GetInstance().SetSSHInfo(self.ssh_info)

    def _GetPublicKey(self, key_dir, key_name):
        return GetPublicKey(key_dir, key_name)

    def _GetPrivateKey(self, key_dir, key_name):
        return GetPrivateKey(key_dir, key_name)
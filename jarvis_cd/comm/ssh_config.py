from jarvis_cd.hostfile import Hostfile
from jarvis_cd.yaml_conf import YAMLConfig
from jarvis_cd.hardware.host_aliases import FindHostAliases

class SSHConfig(YAMLConfig):
    def DefaultConfigPath(self, conf_type='default'):
        return os.path.join(self.jarvis_root, 'comm', f"{conf_type}.yaml")

    def _ProcessConfig(self):
        self.ssh_info = self.config['SSH']
        self.ssh_info['host_aliases'] = FindHostAliases('Get Aliases', self.all_hosts).Run().GetAliases()
        self.hosts = Hostfile().LoadHostfile(self.config['HOSTS'])

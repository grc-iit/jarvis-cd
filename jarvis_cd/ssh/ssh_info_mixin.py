from jarvis_cd.jarvis_manager import JarvisManager
import os

class SSHInfoMixin:
    def _ProcessSSHInfo(self):
        username = None
        pkey = None
        password = os.environ['USER']
        port = None

        ssh_info = JarvisManager.GetInstance().GetSSHInfo()
        host_aliases = ['localhost']

        # Prioritize ssh_info structure
        if ssh_info is not None:
            if 'username' in ssh_info:
                username = ssh_info['username']
            if 'key' in ssh_info:
                if 'key_dir' in ssh_info:
                    pkey = os.path.join(ssh_info['key_dir'], ssh_info['key'])
                else:
                    pkey = ssh_info['key']
            if 'password' in ssh_info:
                password = ssh_info['password']
            if 'port' in ssh_info:
                port = int(ssh_info['port'])
            if 'host_aliases' in ssh_info:
                if isinstance(ssh_info['host_aliases'], list):
                    host_aliases += ssh_info['host_aliases']
                else:
                    host_aliases.append(ssh_info['host_aliases'])

        self.pkey = pkey
        self.password = password
        self.username = username
        self.port = port
        self.ssh_info = ssh_info
        self.host_aliases = host_aliases
        # Do SSH only if the host list contains more than host aliases
        if hasattr(self, 'hosts'):
            self.do_ssh = any(host not in host_aliases for host in self.hosts)
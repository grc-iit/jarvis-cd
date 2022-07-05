import os
from jarvis_cd.hostfile import Hostfile

class SSHArgs:
    def ParseSSHArgs(self):
        self.hosts = []
        self.do_ssh = True
        self.key_dir = os.path.join(os.environ["HOME"], ".ssh")
        self.key_name = "id_rsa"
        self.port = 22
        self.username = os.environ["USER"]

        if "ssh_hosts" in self.conf and self.conf["ssh_hosts"] is not None:
            self.hosts = Hostfile.LoadHostfile(self.conf["ssh_hosts"]).list()
        if "ssh_host" in self.conf and self.conf["ssh_host"] is not None:
            self.hosts.append(self.conf["ssh_host"])
        if len(self.hosts) == 0:
            self.hosts = ['localhost']
            self.do_ssh = False
        self.ssh_keys = {}
        if "username" in self.conf and self.conf["username"] is not None:
            self.username = self.conf["username"]
        if "ssh_port" in self.conf and self.conf["ssh_port"] is not None:
            self.port = int(self.conf["ssh_port"])
        if "ssh_keys" in self.conf:
            self.ssh_keys = self.conf["ssh_keys"]
        self.key_dir, self.key_name, self.dst_key_dir = self.GetKeyInfo('primary')
        self.private_key = self._GetPrivateKey(self.key_dir, self.key_name)
        self.public_key = self._GetPublicKey(self.key_dir, self.key_name)

    def _GetPublicKey(self, key_dir, key_name):
        return f'{key_dir}/{key_name}.pub'

    def _GetPrivateKey(self, key_dir, key_name):
        return f'{key_dir}/{key_name}'

    def GetKeyInfo(self, key_entry):
        key_dir = os.path.join(os.environ["HOME"], ".ssh")
        key_name = 'id_rsa'
        dst_key_dir = os.path.join("home", self.username, ".ssh")
        if key_entry in self.ssh_keys:
            key_dir = self.ssh_keys[key_entry]["key_dir"]
            key_name = self.ssh_keys[key_entry]["key"]
            if 'dst_key_dir' in self.ssh_keys[key_entry]:
                dst_key_dir = self.ssh_keys[key_entry]["dst_key_dir"]
        return key_dir,key_name,dst_key_dir



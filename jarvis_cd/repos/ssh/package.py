from jarvis_cd.shell.exec_node import ExecNode
from jarvis_cd.shell.copy_node import CopyNode
from jarvis_cd.comm.issh_node import InteractiveSSHNode
from jarvis_cd.launcher.launcher import Launcher
import os

class Ssh(Launcher):
    def _ProcessConfig(self):
        super()._ProcessConfig()
        self.dst_key_dir = os.path.join('home', self.username, '.ssh')
        if "ssh_keys" in self.config and "primary" in self.config["ssh_keys"]:
            if "dst_key_dir" in self.config["ssh_keys"]["primary"] and self.config["ssh_keys"]["primary"]["dst_key_dir"] is not None:
                self.dst_key_dir = self.config["ssh_keys"]["primary"]["dst_key_dir"]

    def Copy(self, source, destination):
        CopyNode(source, destination, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def _CopyArgs(self, parser):
        parser.add_argument('source', metavar='path', type=str, help="Source path")
        parser.add_argument('destination', metavar='path', type=str, help="Destination path")

    def Shell(self, node_id):
        InteractiveSSHNode(self.all_hosts.SelectHosts(node_id), self.ssh_info, only_init=True).Run()

    def _ShellArgs(self, parser):
        parser.add_argument('node_id', metavar='id', type=int, help="Node index in hostfile")

    def Exec(self, cmd):
        ExecNode(cmd, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def _ExecArgs(self, parser):
        parser.add_argument('cmd', metavar='command', type=str, help="The command to distribute")

    def Setup(self):
        self._TrustHosts()
        self._InstallKeys()
        self._SSHPermissions()

    def _TrustHosts(self):
        # Ensure all self.all_hosts are trusted on this machine
        print("Add all hosts to known_hosts")
        for host in self.all_hosts:
            InteractiveSSHNode(host, self.ssh_info, only_init=True).Run()

    def _InstallKeys(self):
        print("Install SSH keys")
        # Ensure pubkey trusted on all nodes
        for host in self.all_hosts:
            copy_id_cmd = f"ssh-copy-id -f -i {self.public_key} -p {self.port} {self.username}@{host}"
            ExecNode(copy_id_cmd).Run()
        # Create SSH directory on all nodes
        ExecNode(f'mkdir {self.dst_key_dir}', hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

        # Copy all keys:
        for key_entry in self.ssh_keys.keys():
            key_dir, key_name, dst_key_dir = self._GetKeyInfo(key_entry)
            src_pub_key = self._GetPublicKey(key_dir, key_name)
            src_priv_key = self._GetPrivateKey(key_dir, key_name)
            dst_pub_key = self._GetPublicKey(dst_key_dir, key_name)
            dst_priv_key = self._GetPrivateKey(dst_key_dir, key_name)
            CopyNode(src_pub_key, dst_pub_key, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()
            if os.path.exists(src_priv_key):
                print(f"Copying {src_priv_key} to {dst_priv_key}")
                CopyNode(src_priv_key, dst_priv_key, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def _SSHPermissionsCmd(self, key_location):
        commands = []
        for key_entry in self.ssh_keys.keys():
            key_dir, key_name, dst_key_dir = self._GetKeyInfo(key_entry)
            if key_location == 'remote':
                key_dir = dst_key_dir
            commands += [
                f'chmod 700 {key_dir}',
                f'chmod 600 {key_dir}/authorized_keys',
                f'chmod 644 {key_dir}/known_self.all_hosts',
                f'chmod 600 {key_dir}/config',
                f'chmod 644 {self._GetPublicKey(key_dir, key_name)}',
                f'chmod 600 {self._GetPrivateKey(key_dir, key_name)}'
            ]
        return commands

    def _SSHPermissions(self):
        src_cmd = self._SSHPermissionsCmd('local')
        dst_cmd = self._SSHPermissionsCmd('remote')
        ExecNode(src_cmd, collect_output=False).Run()
        ExecNode(dst_cmd, hosts=self.all_hosts, ssh_info=self.ssh_info).Run()

    def _GetKeyInfo(self, key_entry):
        key_dir = os.path.join(os.environ["HOME"], ".ssh")
        key_name = 'id_rsa'
        dst_key_dir = os.path.join("home", self.username, ".ssh")
        if key_entry in self.ssh_keys:
            key_dir = self.ssh_keys[key_entry]["key_dir"]
            key_name = self.ssh_keys[key_entry]["key"]
            if 'dst_key_dir' in self.ssh_keys[key_entry]:
                dst_key_dir = self.ssh_keys[key_entry]["dst_key_dir"]
        return key_dir,key_name,dst_key_dir
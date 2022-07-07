import argparse
from jarvis_cd.hostfile import Hostfile
from jarvis_cd.basic.exec_node import ExecNode
from jarvis_cd.comm.ssh_node import SSHNode
from jarvis_cd.comm.scp_node import SCPNode
from jarvis_cd.bootstrap.ssh_args import SSHArgs
import sys,os

class SSHSetup(SSHArgs):
    def __init__(self, conf):
        self.conf = conf
        self.ParseSSHArgs()
        self.dst_key_dir = os.path.join('home', self.username, '.ssh')
        if "ssh_keys" in self.conf and "primary" in self.conf["ssh_keys"]:
            if "dst_key_dir" in self.conf["ssh_keys"]["primary"] and self.conf["ssh_keys"]["primary"]["dst_key_dir"] is not None:
                self.dst_key_dir = self.conf["ssh_keys"]["primary"]["dst_key_dir"]

    def Run(self):
        if not self.do_ssh:
            print("Not enough info for SSH specified in conf.yaml")
            exit(1)
        self.TrustHosts()
        self.InstallKeys()
        self.SSHPermissions()

    def TrustHosts(self):
        # Ensure all self.hosts are trusted on this machine
        print("Add all hosts to known_hosts")
        for host in self.hosts:
            ExecNode('trust nodes', f'ssh -i {self.private_key} -p {self.port} {self.username}@{host} echo init').Run()

    def InstallKeys(self):
        print("Install SSH keys")
        # Ensure pubkey trusted on all nodes
        for host in self.hosts:
            copy_id_cmd = f"ssh-copy-id -f -i {self.public_key} -p {self.port} {self.username}@{host}"
            print(copy_id_cmd)
            ExecNode('Install public key', copy_id_cmd, collect_output=False).Run()
        # Create SSH directory on all nodes
        SSHNode('Make SSH directory', self.hosts, f'mkdir {self.dst_key_dir}', pkey=self.private_key, username=self.username, port=self.port,
                collect_output=False).Run()

        # Copy all keys:
        for key_entry in self.ssh_keys.keys():
            key_dir, key_name, dst_key_dir = self.GetKeyInfo(key_entry)
            src_pub_key = self._GetPublicKey(key_dir, key_name)
            src_priv_key = self._GetPrivateKey(key_dir, key_name)
            dst_pub_key = self._GetPublicKey(dst_key_dir, key_name)
            dst_priv_key = self._GetPrivateKey(dst_key_dir, key_name)
            print(f"Copying {src_pub_key} to {dst_pub_key}")
            SCPNode('Copy public key to hosts', self.hosts, src_pub_key, dst_pub_key, pkey=self.private_key, username=self.username,
                    port=self.port,
                    collect_output=False).Run()
            if os.path.exists(src_priv_key):
                print(f"Copying {src_priv_key} to {dst_priv_key}")
                SCPNode('Copy private key to hosts', self.hosts, src_priv_key, dst_priv_key, pkey=self.private_key,
                        username=self.username, port=self.port,
                        collect_output=False).Run()

    def _SSHPermissionsCmd(self, key_location):
        commands = []
        for key_entry in self.ssh_keys.keys():
            key_dir, key_name, dst_key_dir = self.GetKeyInfo(key_entry)
            if key_location == 'remote':
                key_dir = dst_key_dir
            commands += [
                f'chmod 700 {key_dir}',
                f'chmod 600 {key_dir}/authorized_keys',
                f'chmod 644 {key_dir}/known_self.hosts',
                f'chmod 600 {key_dir}/config',
                f'chmod 644 {self._GetPublicKey(key_dir, key_name)}',
                f'chmod 600 {self._GetPrivateKey(key_dir, key_name)}'
            ]
        return commands

    def SSHPermissions(self):
        src_cmd = self._SSHPermissionsCmd('local')
        dst_cmd = self._SSHPermissionsCmd('remote')
        ExecNode('Set permissions locally', src_cmd, collect_output=False).Run()
        SSHNode('Set permissions on destination', self.hosts, dst_cmd, pkey=self.private_key, username=self.username, port=self.port,
                collect_output=False).Run()